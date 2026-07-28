"""Process-wide coordination for concurrent campsite search batches.

A single :class:`SearchDispatcher` owns the worker pool shared by alert and
dashboard scans, so ``--workers`` bounds the number of concurrently executing
search batches across the whole process rather than per scan.

Scheduling semantics:

- Queued alert work is always selected ahead of queued dashboard work, however
  long that dashboard work has waited. Alerts are the latency-sensitive path,
  so they never queue behind dashboard scans.
- Dashboard work may occupy at most ``workers - 1`` slots when ``workers > 1``,
  so one slot always drains toward alert work. With ``workers == 1`` the single
  slot is shared: a dashboard batch may occupy it, and alert work runs next as
  soon as the in-flight batch finishes (strict priority in the queue).
- ``--search-delay`` pacing is tracked per provider inside the dispatcher, so
  overlapping scans share one submission schedule per provider.
- Provider cooldowns are checked when work is dispatched. Queued alert work
  fails fast with :class:`ProviderCooldownActive`: the alert scan re-runs
  within its interval, so returning promptly and retrying beats holding the
  scan open. Queued dashboard work instead waits out a cooldown up to
  ``max_cooldown_wait``, because failing it strands every campground in the
  batch until the next dashboard interval — far longer than the pause itself.
  A cooldown above that budget fails dashboard work too, so a genuine provider
  outage (whose adaptive cooldown grows exponentially) does not stall scans
  behind it.
"""

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .throttle import PROVIDER_THROTTLES, ProviderThrottleRegistry

logger = logging.getLogger(__name__)

PRIORITY_ALERT = 0
PRIORITY_DASHBOARD = 1

# Longest provider cooldown a queued *dashboard* batch will wait out before
# being failed. The adaptive cooldown starts at 30s and doubles per consecutive
# rate limit, so this waits out the first couple of levels — the common
# transient case — and gives up once the provider looks genuinely unhappy.
# Alert work is never held for a cooldown; see the module docstring.
DEFAULT_MAX_COOLDOWN_WAIT_SECONDS = 60.0


class ProviderCooldownActive(Exception):
    """Raised on a queued search's future when its provider cooldown is active."""

    def __init__(self, provider: str, cooldown_seconds: float):
        super().__init__(f"provider {provider} cooldown active ({cooldown_seconds:.0f}s remaining)")
        self.provider = provider
        self.cooldown_seconds = cooldown_seconds


@dataclass(slots=True)
class _PendingWork:
    fn: Callable[[], object]
    provider: str
    priority: int
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    seq: int = 0


class SearchDispatcher:
    """Priority-aware dispatcher enforcing a process-wide search worker limit."""

    def __init__(
        self,
        workers: int = 2,
        search_delay: float = 0.0,
        *,
        throttles: ProviderThrottleRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_cooldown_wait: float = DEFAULT_MAX_COOLDOWN_WAIT_SECONDS,
    ):
        self.workers = max(1, int(workers))
        self.search_delay = max(0.0, float(search_delay))
        self.throttles = throttles if throttles is not None else PROVIDER_THROTTLES
        self.max_cooldown_wait = max(0.0, float(max_cooldown_wait))
        self._clock = clock
        self._cond = threading.Condition()
        self._pending: list[_PendingWork] = []
        self._active_total = 0
        self._active_by_priority = {PRIORITY_ALERT: 0, PRIORITY_DASHBOARD: 0}
        self._next_submission: dict[str, float] = {}
        self._seq = 0
        self._shutdown = False
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="search-worker",
        )
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="search-dispatcher",
            daemon=True,
        )
        self._thread.start()

    @property
    def dashboard_slots(self) -> int:
        """Concurrent dashboard batch cap; the shared slot when workers == 1."""
        return 1 if self.workers == 1 else self.workers - 1

    @property
    def is_shutdown(self) -> bool:
        with self._cond:
            return self._shutdown

    def matches(
        self,
        workers: int,
        search_delay: float,
        throttles: ProviderThrottleRegistry,
    ) -> bool:
        return (
            self.workers == max(1, int(workers))
            and self.search_delay == max(0.0, float(search_delay))
            and self.throttles is throttles
        )

    def submit(
        self,
        fn: Callable[[], object],
        *,
        provider: str,
        priority: int = PRIORITY_ALERT,
    ) -> concurrent.futures.Future:
        if priority not in (PRIORITY_ALERT, PRIORITY_DASHBOARD):
            raise ValueError(f"unknown priority: {priority!r}")
        work = _PendingWork(fn=fn, provider=provider, priority=priority)
        with self._cond:
            if self._shutdown:
                raise RuntimeError("SearchDispatcher is shut down")
            work.seq = self._seq
            self._seq += 1
            self._pending.append(work)
            self._cond.notify_all()
        return work.future

    def kick(self) -> None:
        """Wake the dispatch loop; used after external clock or cooldown changes."""
        with self._cond:
            self._cond.notify_all()

    def shutdown(self, wait: bool = True) -> None:
        with self._cond:
            already_down = self._shutdown
            self._shutdown = True
            pending, self._pending = self._pending, []
            self._cond.notify_all()
        for work in pending:
            if work.future.cancel():
                # Move the future to CANCELLED_AND_NOTIFIED so waiters
                # (e.g. as_completed) observe the cancellation.
                work.future.set_running_or_notify_cancel()
        if not already_down:
            self._thread.join(timeout=5.0)
        self._pool.shutdown(wait=wait, cancel_futures=True)

    # --- dispatch loop -----------------------------------------------------

    def _dispatch_loop(self) -> None:
        with self._cond:
            while not self._shutdown:
                self._fail_cooldown_blocked_locked()
                index, wait_seconds = self._select_locked(self._clock())
                if index is not None:
                    self._dispatch_locked(index)
                    continue
                self._cond.wait(wait_seconds)

    def _cooldown_wait_budget(self, priority: int) -> float:
        """How long queued work of ``priority`` may wait out a provider cooldown.

        Alert work gets no budget: its scan re-runs within the alert interval,
        so failing fast and retrying beats holding the scan open. Dashboard
        work waits, because failing it strands the whole batch until the next
        dashboard interval.
        """
        return 0.0 if priority == PRIORITY_ALERT else self.max_cooldown_wait

    def _fail_cooldown_blocked_locked(self) -> None:
        """Fail queued work whose provider cooldown outlasts its wait budget.

        Shorter cooldowns stay pending and are waited out by
        :meth:`_select_locked`.
        """
        still_pending: list[_PendingWork] = []
        for work in self._pending:
            cooldown = self.throttles.cooldown_seconds(work.provider)
            if cooldown > self._cooldown_wait_budget(work.priority):
                if work.future.set_running_or_notify_cancel():
                    work.future.set_exception(ProviderCooldownActive(work.provider, cooldown))
            else:
                still_pending.append(work)
        self._pending = still_pending

    def _select_locked(self, now: float) -> tuple[int | None, float | None]:
        """Pick the next dispatchable item, or how long to wait for pacing."""
        if self._active_total >= self.workers:
            return None, None
        best_index: int | None = None
        best_key: tuple[int, int] | None = None
        wait_seconds: float | None = None
        for index, work in enumerate(self._pending):
            if (
                work.priority == PRIORITY_DASHBOARD
                and self._active_by_priority[PRIORITY_DASHBOARD] >= self.dashboard_slots
            ):
                continue
            # A cooldown short enough to survive _fail_cooldown_blocked_locked
            # is waited out here, exactly like --search-delay pacing. Alert work
            # has a zero budget, so it is never pending at this point with a
            # cooldown active.
            cooldown = self.throttles.cooldown_seconds(work.provider)
            ready_at = max(self._next_submission.get(work.provider, 0.0), now + cooldown)
            if ready_at > now:
                pacing_wait = ready_at - now
                wait_seconds = (
                    pacing_wait if wait_seconds is None else min(wait_seconds, pacing_wait)
                )
                continue
            key = (work.priority, work.seq)
            if best_key is None or key < best_key:
                best_index, best_key = index, key
        return best_index, wait_seconds

    def _dispatch_locked(self, index: int) -> None:
        work = self._pending.pop(index)
        if not work.future.set_running_or_notify_cancel():
            return
        self._active_total += 1
        self._active_by_priority[work.priority] += 1
        self._next_submission[work.provider] = self._clock() + self.search_delay
        self._pool.submit(self._run, work)

    def _run(self, work: _PendingWork) -> None:
        try:
            result = work.fn()
        except BaseException as exc:  # propagate any failure through the future
            work.future.set_exception(exc)
        else:
            work.future.set_result(result)
        finally:
            with self._cond:
                self._active_total -= 1
                self._active_by_priority[work.priority] -= 1
                self._cond.notify_all()


_GLOBAL_DISPATCHER: SearchDispatcher | None = None
_GLOBAL_LOCK = threading.Lock()


def get_dispatcher(
    workers: int,
    search_delay: float,
    throttles: ProviderThrottleRegistry,
) -> SearchDispatcher:
    """Return the process-wide dispatcher, (re)creating it if config changed.

    In production every scan passes the same CLI-derived values, so the
    dispatcher is created once and shared for the life of the process.
    """
    global _GLOBAL_DISPATCHER
    with _GLOBAL_LOCK:
        current = _GLOBAL_DISPATCHER
        if current is not None and (
            current.is_shutdown or not current.matches(workers, search_delay, throttles)
        ):
            if not current.is_shutdown:
                logger.warning(
                    "Search dispatcher reconfigured (workers=%s, search_delay=%s)",
                    workers,
                    search_delay,
                )
            current.shutdown(wait=False)
            _GLOBAL_DISPATCHER = None
        if _GLOBAL_DISPATCHER is None:
            _GLOBAL_DISPATCHER = SearchDispatcher(
                workers=workers,
                search_delay=search_delay,
                throttles=throttles,
            )
        return _GLOBAL_DISPATCHER


def shutdown_dispatcher(wait: bool = False) -> None:
    """Shut down the process-wide dispatcher, if one exists."""
    global _GLOBAL_DISPATCHER
    with _GLOBAL_LOCK:
        dispatcher, _GLOBAL_DISPATCHER = _GLOBAL_DISPATCHER, None
    if dispatcher is not None:
        dispatcher.shutdown(wait=wait)
