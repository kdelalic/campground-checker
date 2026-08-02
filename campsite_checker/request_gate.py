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
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Hashable, TypeVar, cast

from .throttle import detect_rate_limit

logger = logging.getLogger(__name__)

# Mirrors dispatch.PRIORITY_ALERT / PRIORITY_DASHBOARD without importing the
# dispatcher into the provider layer.
PRIORITY_ALERT_REQUEST = 0
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 30.0
_T = TypeVar("_T")


class RequestDeferredError(Exception):
    """Raised when latency-sensitive work refuses to wait out a gate pause."""

    def __init__(self, seconds: float):
        super().__init__(f"provider request temporarily paused ({seconds:.0f}s remaining)")
        self.seconds = seconds


class SharedFetchError(Exception):
    """A coalesced follower failed because the fetch owner raised an error.

    The owner's original exception remains the single source of provider
    throttle classification. Reclassifying the same shared 429 in every
    follower would inflate rate-limit counters and exponential backoff.
    """

    def __init__(self):
        super().__init__("coalesced provider fetch failed; retry on the next scan")


@dataclass(slots=True)
class _InFlightFetch:
    event: threading.Event
    value: object | None = None
    error: BaseException | None = None


class SingleFlightTTLCache:
    """Bounded response cache that coalesces concurrent identical fetches.

    Provider clients use a cache shorter than the alert interval, so criteria
    from the same scan can share raw availability without carrying a snapshot
    into the next alert cycle. Failed fetches are never cached.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        max_entries: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if max_entries < 1:
            raise ValueError("max_entries must be at least one")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, object]] = OrderedDict()
        self._in_flight: dict[Hashable, _InFlightFetch] = {}

    def get_or_fetch(self, key: Hashable, fetch: Callable[[], _T]) -> _T:
        """Return a fresh value, with one caller owning an uncached fetch."""
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                expires_at, value = cached
                if expires_at > self._clock():
                    self._entries.move_to_end(key)
                    return cast(_T, value)
                del self._entries[key]

            in_flight = self._in_flight.get(key)
            owner = in_flight is None
            if owner:
                in_flight = _InFlightFetch(event=threading.Event())
                self._in_flight[key] = in_flight

        if not owner:
            in_flight.event.wait()
            if in_flight.error is not None:
                raise SharedFetchError
            return cast(_T, in_flight.value)

        try:
            value = fetch()
        except BaseException as exc:
            with self._lock:
                in_flight.error = exc
                self._in_flight.pop(key, None)
                in_flight.event.set()
            raise

        with self._lock:
            in_flight.value = value
            self._entries[key] = (self._clock() + self.ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self._in_flight.pop(key, None)
            in_flight.event.set()
        return value

    def clear(self) -> None:
        """Discard completed entries without interrupting active fetches."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


@dataclass(frozen=True, slots=True)
class ProviderRequestSnapshot:
    provider: str
    attempts: int
    retries: int
    failures: int


class ProviderRequestMetrics:
    """Small in-process counter registry rendered by the existing metrics endpoint.

    Shared by every native provider client, so it lives beside the gate rather
    than inside one provider's module.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[str, list[int]] = {}

    def _increment(self, provider: str, index: int) -> None:
        with self._lock:
            values = self._values.setdefault(provider, [0, 0, 0])
            values[index] += 1

    def record_attempt(self, provider: str) -> None:
        self._increment(provider, 0)

    def record_retry(self, provider: str) -> None:
        self._increment(provider, 1)

    def record_failure(self, provider: str) -> None:
        self._increment(provider, 2)

    def snapshot(self) -> list[ProviderRequestSnapshot]:
        with self._lock:
            return [
                ProviderRequestSnapshot(
                    provider=provider,
                    attempts=values[0],
                    retries=values[1],
                    failures=values[2],
                )
                for provider, values in sorted(self._values.items())
            ]

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


PROVIDER_REQUEST_METRICS = ProviderRequestMetrics()


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
        self._deferred_until = 0.0

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
            deferred_until = self._clock() + max(0.0, seconds)
            self._deferred_until = max(self._deferred_until, deferred_until)
            self._next_start = max(self._next_start, deferred_until)
            self._cond.notify_all()

    @contextmanager
    def slot(
        self,
        priority: int = PRIORITY_ALERT_REQUEST,
        *,
        fail_when_deferred: bool = False,
    ):
        self._acquire(priority, fail_when_deferred=fail_when_deferred)
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

    def _acquire(self, priority: int, *, fail_when_deferred: bool = False) -> None:
        with self._cond:
            ticket = (priority, self._seq)
            self._seq += 1
            heapq.heappush(self._waiting, ticket)
            self._cond.notify_all()
        try:
            while True:
                with self._cond:
                    deferred_seconds = max(0.0, self._deferred_until - self._clock())
                    if fail_when_deferred and deferred_seconds:
                        self._waiting.remove(ticket)
                        heapq.heapify(self._waiting)
                        self._cond.notify_all()
                        raise RequestDeferredError(deferred_seconds)
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
