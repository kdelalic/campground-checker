"""Priority-aware, process-wide gate for outbound provider requests.

Alert scans are the latency-sensitive path, so a queued alert request must take
the next available request start even when dashboard requests have been waiting
longer. The dispatcher already orders *batches* that way; this gate applies the
same rule to the individual HTTP requests inside a batch, which is what keeps
an alert fast while a long dashboard sweep is mid-flight.

Each provider owns its own gate instance because the limits being respected
(concurrency, spacing, cooldowns) are per-provider.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable

from .throttle import detect_rate_limit

logger = logging.getLogger(__name__)

# Mirrors dispatch.PRIORITY_ALERT / PRIORITY_DASHBOARD without importing the
# dispatcher into the provider layer.
PRIORITY_ALERT_REQUEST = 0
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 30.0


class RequestGate:
    """Order request starts by priority under a shared concurrency/rate limit.

    Waiters are queued by ``(priority, arrival)`` — lower priority wins — so a
    queued alert request takes the next available start even when dashboard
    requests have been waiting longer. One concurrency slot is also held back
    for alert requests, so an alert does not have to wait for an in-flight
    dashboard request to finish; see :attr:`deprioritized_slots`. Aggregate
    start spacing is preserved, and :meth:`defer` pauses every future start so
    batches that are already executing stop issuing requests during a provider
    cooldown.

    An in-flight request is never preempted, so with ``max_concurrent == 1``
    an alert may still wait for the one outstanding request, bounded by the
    client's connect/read timeouts.
    """

    # Safety net so a missed notification degrades to a short re-check rather
    # than a hang; all state changes also notify waiters.
    _WAIT_TIMEOUT_SECONDS = 0.05

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        requests_per_second: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._max_concurrent = max(1, int(max_concurrent))
        self._active_deprioritized = 0
        # ``None`` bounds concurrency only: useful for providers that are not
        # known to rate limit, where the queue exists purely to order alerts
        # ahead of dashboard requests.
        self._minimum_interval = 1 / requests_per_second if requests_per_second else 0.0
        self._clock = clock
        self._sleep = sleep
        self._cond = threading.Condition()
        self._waiting: list[tuple[int, int]] = []
        self._active = 0
        self._seq = 0
        self._next_start = 0.0

    @property
    def deprioritized_slots(self) -> int:
        """Concurrent slots usable by non-alert requests.

        One slot is held back for alert requests whenever there is more than
        one, mirroring the dispatcher's reserved worker. Ordering alone is not
        enough: without a reserved slot an alert still waits for one of the
        in-flight dashboard requests to finish, and on a provider whose
        requests take seconds that wait dominates the whole alert scan.
        """
        return 1 if self._max_concurrent == 1 else self._max_concurrent - 1

    def _can_start_locked(self, priority: int) -> bool:
        if self._active >= self._max_concurrent:
            return False
        if priority > PRIORITY_ALERT_REQUEST:
            return self._active_deprioritized < self.deprioritized_slots
        return True

    @property
    def pending_priorities(self) -> tuple[int, ...]:
        """Priorities of the currently queued waiters, most favoured first."""
        with self._cond:
            return tuple(sorted(priority for priority, _seq in self._waiting))

    def defer(self, seconds: float) -> None:
        """Pause every future request start for at least ``seconds``."""
        with self._cond:
            self._next_start = max(self._next_start, self._clock() + max(0.0, seconds))
            self._cond.notify_all()

    @contextmanager
    def slot(self, priority: int = PRIORITY_ALERT_REQUEST):
        self._acquire(priority)
        try:
            yield self
        finally:
            self._release(priority)

    def __enter__(self):
        self._acquire(PRIORITY_ALERT_REQUEST)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._release(PRIORITY_ALERT_REQUEST)
        return False

    def _acquire(self, priority: int) -> None:
        with self._cond:
            ticket = (priority, self._seq)
            self._seq += 1
            heapq.heappush(self._waiting, ticket)
            self._cond.notify_all()
        try:
            while True:
                with self._cond:
                    if self._waiting[0] == ticket and self._can_start_locked(priority):
                        delay = max(0.0, self._next_start - self._clock())
                        if not delay:
                            heapq.heappop(self._waiting)
                            self._active += 1
                            if priority > PRIORITY_ALERT_REQUEST:
                                self._active_deprioritized += 1
                            self._next_start = self._clock() + self._minimum_interval
                            self._cond.notify_all()
                            return
                    else:
                        delay = None
                    if delay is None:
                        self._cond.wait(self._WAIT_TIMEOUT_SECONDS)
                        continue
                # Only the favoured waiter sleeps out the start spacing, and it
                # does so outside the lock so releases and defers stay prompt.
                self._sleep(delay)
        except BaseException:
            with self._cond:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                    heapq.heapify(self._waiting)
                    self._cond.notify_all()
            raise

    def _release(self, priority: int) -> None:
        with self._cond:
            self._active -= 1
            if priority > PRIORITY_ALERT_REQUEST:
                self._active_deprioritized -= 1
            self._cond.notify_all()


def pause_gate_on_rate_limit(gate: RequestGate, exc: BaseException, *, provider: str) -> None:
    """Pause ``gate`` when ``exc`` is a rate limit, honoring ``Retry-After``.

    Called while the failing request still holds its slot, so batches that are
    already executing stop issuing requests immediately instead of each
    discovering the limit itself. This complements the batch-level provider
    cooldown in :mod:`campsite_checker.throttle`, which only gates work when a
    batch is dispatched.
    """
    detection = detect_rate_limit(exc)
    if not detection.rate_limited:
        return
    pause = detection.retry_after_seconds or DEFAULT_RATE_LIMIT_PAUSE_SECONDS
    gate.defer(pause)
    logger.warning("%s rate limited; pausing all requests for %.0fs", provider, pause)
