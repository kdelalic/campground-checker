"""Native Recreation.gov availability search.

Camply remains the source of facility identity records for now, but the
latency-sensitive availability path is implemented here. In particular, this
client does not inherit Camply's 100-minute retry window.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import requests
from camply.containers import AvailableCampsite, CampgroundFacility, SearchWindow
from camply.providers import RecreationDotGov

from ..throttle import detect_rate_limit

logger = logging.getLogger(__name__)

AVAILABILITY_URL = (
    "https://www.recreation.gov/api/camps/availability/campground/{facility_id}/month"
)
CAMPSITE_BOOKING_URL = "https://www.recreation.gov/camping/campsites/{campsite_id}"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_READ_TIMEOUT_SECONDS = 7
DEFAULT_REQUEST_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAYS_SECONDS = (1.0, 2.0)
REQUESTS_PER_SECOND = 1
MAX_CONCURRENT_REQUESTS = 2
# Mirrors dispatch.PRIORITY_ALERT / PRIORITY_DASHBOARD without importing the
# dispatcher into the provider layer.
PRIORITY_ALERT_REQUEST = 0
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 30.0

# These are Camply's current values, copied deliberately so unknown future
# statuses continue to surface as potentially bookable instead of silently
# hiding inventory.
UNAVAILABLE_STATUSES = frozenset(
    {
        "Reserved",
        "Not Available",
        "Not Reservable",
        "Not Reservable Management",
        "Not Available Cutoff",
        "Lottery",
        "Open",
        "NYR",
        "Closed",
    }
)

REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.recreation.gov/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


class FacilityIdentityCache:
    """Thread-safe bounded TTL cache for resolved facility identity records."""

    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: float = 24 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, CampgroundFacility]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, facility_id: str) -> CampgroundFacility | None:
        with self._lock:
            cached = self._entries.get(facility_id)
            if cached is None:
                return None
            expires_at, facility = cached
            if expires_at <= self._clock():
                del self._entries[facility_id]
                return None
            self._entries.move_to_end(facility_id)
            return facility

    def store(self, facility_id: str, facility: CampgroundFacility) -> None:
        with self._lock:
            self._entries[facility_id] = (self._clock() + self.ttl_seconds, facility)
            self._entries.move_to_end(facility_id)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


FACILITY_IDENTITY_CACHE = FacilityIdentityCache()


class IdentityCachedRecreationDotGov(RecreationDotGov):
    """Camply RIDB identity lookup with a shared process-local cache."""

    def _find_facilities_from_campgrounds(self, campground_id):
        requested = (
            list(campground_id) if isinstance(campground_id, (list, tuple)) else [campground_id]
        )
        resolved: dict[str, CampgroundFacility] = {}
        misses = []
        for identifier in requested:
            facility = FACILITY_IDENTITY_CACHE.get(str(identifier))
            if facility is None:
                misses.append(identifier)
            else:
                resolved[str(identifier)] = facility
        if misses:
            for facility in super()._find_facilities_from_campgrounds(misses):
                key = str(facility.facility_id)
                resolved[key] = facility
                FACILITY_IDENTITY_CACHE.store(key, facility)
        return [
            resolved[str(identifier)] for identifier in requested if str(identifier) in resolved
        ]


@dataclass(frozen=True, slots=True)
class ProviderRequestSnapshot:
    provider: str
    attempts: int
    retries: int
    failures: int


class ProviderRequestMetrics:
    """Small in-process counter registry rendered by the existing metrics endpoint."""

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
    """Order Recreation.gov request starts by priority under a shared rate limit.

    Waiters are queued by ``(priority, arrival)`` — lower priority wins — so a
    queued alert request takes the next available start even when dashboard
    requests have been waiting longer. Aggregate start spacing and the
    concurrency cap are preserved, and :meth:`defer` pauses every future start
    so batches that are already executing stop issuing requests during a
    provider cooldown.

    An in-flight request is never preempted: an alert may still wait for one
    outstanding request, bounded by the client's connect/read timeouts.
    """

    # Safety net so a missed notification degrades to a short re-check rather
    # than a hang; all state changes also notify waiters.
    _WAIT_TIMEOUT_SECONDS = 0.05

    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT_REQUESTS,
        requests_per_second: float = REQUESTS_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._max_concurrent = max(1, int(max_concurrent))
        self._minimum_interval = 1 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._cond = threading.Condition()
        self._waiting: list[tuple[int, int]] = []
        self._active = 0
        self._seq = 0
        self._next_start = 0.0

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
            self._release()

    def __enter__(self):
        self._acquire(PRIORITY_ALERT_REQUEST)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._release()
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
                    if self._active < self._max_concurrent and self._waiting[0] == ticket:
                        delay = max(0.0, self._next_start - self._clock())
                        if not delay:
                            heapq.heappop(self._waiting)
                            self._active += 1
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

    def _release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()


REC_GOV_REQUEST_GATE = RequestGate()


def _as_list(value) -> list:
    if value in (None, (), []):
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _parse_booking_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _month_starts(search_days: set[date]) -> list[date]:
    return sorted({day.replace(day=1) for day in search_days})


class NativeSearchRecreationDotGov:
    """Direct Recreation.gov availability search with bounded retries."""

    provider_class = IdentityCachedRecreationDotGov
    provider_name = "RecreationDotGov"

    def __init__(
        self,
        search_window: SearchWindow | list[SearchWindow],
        recreation_area: list[int] | int | None = None,
        campgrounds: list[int] | int | None = None,
        campsites: list[int] | int | None = None,
        weekends_only: bool = False,
        nights: int = 1,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_gate: RequestGate | None = None,
        request_metrics: ProviderRequestMetrics | None = None,
        request_priority: int = PRIORITY_ALERT_REQUEST,
        **_kwargs,
    ):
        if not any((campgrounds, recreation_area, campsites)):
            raise ValueError("A campground, recreation area, or campsite ID is required")
        if nights < 1:
            raise ValueError("nights must be at least 1")

        self.search_window = _as_list(search_window)
        self.weekends_only = weekends_only
        self.nights = nights
        self.campsites = {str(identifier) for identifier in _as_list(campsites)}
        self.session = session or requests.Session()
        self._sleep = sleep
        self._request_gate = request_gate or REC_GOV_REQUEST_GATE
        self._request_metrics = request_metrics or PROVIDER_REQUEST_METRICS
        self.request_priority = request_priority

        provider = self.provider_class()
        self.campgrounds = provider.find_campgrounds(
            rec_area_id=_as_list(recreation_area),
            campground_id=_as_list(campgrounds),
            campsite_id=_as_list(campsites),
        )

    def _search_days(self) -> set[date]:
        today = date.today()
        days: set[date] = set()
        allowed_weekdays = {4, 5} if self.weekends_only else set(range(7))
        for window in self.search_window:
            day = max(window.start_date, today)
            while day < window.end_date:
                if day.weekday() in allowed_weekdays:
                    days.add(day)
                day += timedelta(days=1)
        return days

    def _pause_gate_if_rate_limited(self, response) -> None:
        """Raise for error responses, deferring the shared gate on HTTP 429."""
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detection = detect_rate_limit(exc)
            if detection.rate_limited:
                pause = detection.retry_after_seconds or DEFAULT_RATE_LIMIT_PAUSE_SECONDS
                self._request_gate.defer(pause)
                logger.warning(
                    "Recreation.gov rate limited; pausing all requests for %.0fs",
                    pause,
                )
            raise

    def _request_month(self, facility_id: int | str, month: date) -> dict:
        url = AVAILABILITY_URL.format(facility_id=facility_id)
        params = {"start_date": month.strftime("%Y-%m-01T00:00:00.000Z")}
        last_error: Exception | None = None

        for attempt in range(DEFAULT_MAX_ATTEMPTS):
            self._request_metrics.record_attempt(self.provider_name)
            try:
                with self._request_gate.slot(self.request_priority):
                    response = self.session.get(
                        url,
                        params=params,
                        headers=REQUEST_HEADERS,
                        timeout=DEFAULT_REQUEST_TIMEOUT,
                    )
                    # Pause the shared gate before releasing the slot so batches
                    # already executing stop issuing requests immediately,
                    # rather than after their own next 429.
                    self._pause_gate_if_rate_limited(response)
                return response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                retryable = True
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                retryable = status_code is not None and 500 <= status_code < 600
            except (TypeError, ValueError) as exc:
                last_error = exc
                retryable = False

            if not retryable or attempt == DEFAULT_MAX_ATTEMPTS - 1:
                self._request_metrics.record_failure(self.provider_name)
                raise last_error

            delay = DEFAULT_RETRY_DELAYS_SECONDS[attempt]
            self._request_metrics.record_retry(self.provider_name)
            logger.warning(
                "Recreation.gov request failed for facility %s (%s); retrying in %.1fs",
                facility_id,
                last_error,
                delay,
            )
            self._sleep(delay)

        raise RuntimeError("unreachable")

    @staticmethod
    def _make_result(
        *,
        campground: CampgroundFacility,
        campsite_id: str,
        site: dict,
        booking_date: datetime,
        nights: int,
    ) -> AvailableCampsite:
        return AvailableCampsite(
            campsite_id=campsite_id,
            booking_date=booking_date,
            booking_end_date=booking_date + timedelta(days=nights),
            booking_nights=nights,
            campsite_site_name=site.get("site") or campsite_id,
            campsite_loop_name=site.get("loop"),
            campsite_type=site.get("campsite_type"),
            campsite_occupancy=(
                site.get("min_num_people") or 0,
                site.get("max_num_people") or 0,
            ),
            campsite_use_type=site.get("type_of_use"),
            availability_status="Available",
            recreation_area=campground.recreation_area or "",
            recreation_area_id=campground.recreation_area_id or 0,
            facility_name=campground.facility_name or "",
            facility_id=campground.facility_id,
            booking_url=CAMPSITE_BOOKING_URL.format(campsite_id=campsite_id),
            permitted_equipment=[],
            campsite_attributes=[],
            location=None,
        )

    def _results_for_campground(
        self,
        campground: CampgroundFacility,
        search_days: set[date],
    ) -> list[AvailableCampsite]:
        sites_by_id: dict[str, dict] = {}
        availability_by_site: dict[str, dict[date, str]] = defaultdict(dict)

        for month in _month_starts(search_days):
            payload = self._request_month(campground.facility_id, month)
            for campsite_id, site in payload.get("campsites", {}).items():
                campsite_id = str(campsite_id)
                if self.campsites and campsite_id not in self.campsites:
                    continue
                sites_by_id[campsite_id] = site
                for value, status in site.get("availabilities", {}).items():
                    booking_date = _parse_booking_date(value).date()
                    if booking_date in search_days and status not in UNAVAILABLE_STATUSES:
                        availability_by_site[campsite_id][booking_date] = status

        results = []
        for campsite_id, available_dates in availability_by_site.items():
            for start in sorted(available_dates):
                stay_dates = {start + timedelta(days=offset) for offset in range(self.nights)}
                if not stay_dates.issubset(available_dates):
                    continue
                result = self._make_result(
                    campground=campground,
                    campsite_id=campsite_id,
                    site=sites_by_id[campsite_id],
                    booking_date=datetime.combine(start, datetime.min.time()),
                    nights=self.nights,
                )
                results.append(result)
        return results

    def get_matching_campsites(
        self,
        log: bool = True,
        verbose: bool = False,
        continuous: bool = False,
        **_kwargs,
    ) -> list[AvailableCampsite]:
        if continuous:
            raise ValueError("Native Recreation.gov search only supports one-shot scans")
        if not self.campgrounds:
            raise RuntimeError("No campgrounds found to search")

        search_days = self._search_days()
        if not search_days:
            return []

        results = []
        for campground in self.campgrounds:
            results.extend(self._results_for_campground(campground, search_days))
        if log:
            logger.info(
                "%d Recreation.gov campsite-date combinations matched the search",
                len(results),
            )
        return results
