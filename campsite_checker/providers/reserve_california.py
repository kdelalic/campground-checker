"""Native ReserveCalifornia (UseDirect) availability search.

Camply remains the source of facility identity and unit metadata, but the
availability path is implemented here, for the same reason as Recreation.gov
plus one of its own: camply's UseDirect search asks the grid endpoint for a
calendar month at a time, and the endpoint silently truncates every response to
21 days, so days 22..EOM of every month were never checked.

The camply provider class is still used (and still hardened) for the identity
and metadata lookups this client builds on.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Callable

import requests
from camply.config.api_config import UseDirectConfig
from camply.containers import AvailableCampsite, CampgroundFacility, SearchWindow
from camply.containers.data_containers import CampsiteLocation
from camply.providers.base_provider import ProviderError
from camply.providers.usedirect.variations import ReserveCalifornia

from ..request_gate import (
    PRIORITY_ALERT_REQUEST,
    PROVIDER_REQUEST_METRICS,
    ProviderRequestMetrics,
    RequestGate,
    pause_gate_on_rate_limit,
)

logger = logging.getLogger(__name__)

# Applied to the camply identity/metadata calls, whose stock HTTP path carries
# no timeout; a black-holed connection would otherwise hang a scan thread
# forever.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30

# The availability path uses split timeouts like the Recreation.gov client. The
# read budget is looser because the grid endpoint returns the whole facility
# (~90 KB for a large campground) rather than one month of one calendar.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_READ_TIMEOUT_SECONDS = 10
DEFAULT_REQUEST_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAYS_SECONDS = (1.0, 2.0)

# The grid endpoint caps every response at 21 days of slices, whatever EndDate
# asks for, and honours shorter ranges exactly. Camply's search loops over
# calendar months and so silently loses the tail of each one; windows here are
# sized to the cap instead.
MAX_GRID_WINDOW_DAYS = 21

# Bound how many requests are in flight and let alert requests take the next
# start. One slot is reserved for alert requests, leaving dashboard scans two
# concurrent requests. Ordering alone was not enough in production: an alert
# that waits for an in-flight dashboard request to finish still lost time
# across a multi-request scan.
MAX_CONCURRENT_REQUESTS = 3

# No ReserveCalifornia rate limit is documented or observed, but camply's
# availability call carried a process-global `1 request/second` decorator that
# this client no longer goes through. Pacing is kept — deliberately looser than
# camply's, since the serial month loop is gone — so dropping that decorator
# does not turn into an unbounded increase in request rate.
REQUESTS_PER_SECOND = 4

# Alert requests get fewer rate-limit retries than dashboard requests: a
# rate-limited alert scan re-runs within `--alert-interval` anyway, so it is
# better to return late-but-fresh on the next cycle than to stack multiple
# provider pauses onto one latency-sensitive scan.
DEFAULT_MAX_RATE_LIMIT_RETRIES = 2
ALERT_MAX_RATE_LIMIT_RETRIES = 1

RESERVE_CALIFORNIA_REQUEST_GATE = RequestGate(
    max_concurrent=MAX_CONCURRENT_REQUESTS,
    requests_per_second=REQUESTS_PER_SECOND,
)

# Camply's UseDirect providers default their offline metadata cache to a
# directory beside the installed package, which is read-only in the container
# (the process runs as an unprivileged user). The cache must live somewhere
# writable; the container sets CAMPLY_CACHE_DIR to the persistent state mount.
CAMPLY_CACHE_DIR_ENV = "CAMPLY_CACHE_DIR"
DEFAULT_CAMPLY_CACHE_DIR = pathlib.Path(".camply-cache")

# Matches camply's own expiry for this cache, which never fires on the paths
# this project uses; see `TimeoutReserveCalifornia.refresh_stale_metadata`.
METADATA_MAX_AGE_SECONDS = 24 * 60 * 60

# Serialises the re-fetch so concurrent searches that all notice the same stale
# cache do not each re-download it.
_METADATA_REFRESH_LOCK = threading.Lock()

GRID_URL = (
    f"{ReserveCalifornia.base_url}/"
    f"{ReserveCalifornia.rdr_path}{UseDirectConfig.AVAILABILITY_ENDPOINT}"
)
REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.reservecalifornia.com/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def usedirect_cache_dir(provider_slug: str) -> pathlib.Path:
    """Writable offline-cache directory for a UseDirect provider."""
    base = os.environ.get(CAMPLY_CACHE_DIR_ENV)
    root = pathlib.Path(base) if base else DEFAULT_CAMPLY_CACHE_DIR
    return root / provider_slug


class TimeoutReserveCalifornia(ReserveCalifornia):
    """ReserveCalifornia provider whose HTTP requests always carry a timeout.

    Camply's UseDirect providers call ``session.request`` without a timeout
    (unlike its Recreation.gov, GoingToCamp, and Yellowstone providers, which
    pass ``timeout=30``). Without one, a dropped connection blocks the search
    thread indefinitely and silently stalls the background dashboard worker.
    Mirrors ``BaseProvider.make_http_request``; ``make_http_request_retry``
    delegates here, so every UseDirect HTTP path gets the timeout.

    The offline metadata cache is also relocated: camply defaults it to
    ``site-packages/camply/providers/usedirect/<ClassName>``, which the
    unprivileged container user cannot create. Every metadata refresh goes
    through ``offline_cache_dir``, so overriding the property is sufficient.

    Availability now goes through :class:`NativeSearchReserveCalifornia`, so
    what remains on this path is facility identity and unit metadata. Those
    requests still share the gate, at the priority of the scan that triggered
    them.
    """

    request_gate = RESERVE_CALIFORNIA_REQUEST_GATE
    request_priority = PRIORITY_ALERT_REQUEST

    @property
    def offline_cache_dir(self) -> pathlib.Path:
        return usedirect_cache_dir("reserve-california")

    def _metadata_is_stale(self) -> bool:
        ages = [
            time.time() - cached.stat().st_mtime for cached in self.offline_cache_dir.glob("*.json")
        ]
        return any(age > METADATA_MAX_AGE_SECONDS for age in ages)

    def refresh_stale_metadata(self) -> None:
        """Re-fetch the offline metadata once it ages out.

        Camply expires this cache after a day, but only when ``active_search``
        is False — and ``find_campgrounds`` sets it True *before* refreshing,
        so on every path this project uses the expiry never fires. Because
        `CAMPLY_CACHE_DIR` is a persistent volume in the container, that froze
        the cache at whatever the first run downloaded: campgrounds added by
        ReserveCalifornia stayed unfindable and renamed parks kept their old
        names indefinitely.
        """
        if not self._metadata_is_stale():
            return
        with _METADATA_REFRESH_LOCK:
            # Another thread may have refreshed it while this one waited.
            if not self._metadata_is_stale():
                return
            logger.info("ReserveCalifornia metadata is stale; refreshing offline cache")
            self.metadata_refreshed = False
            self.active_search = False
            self.refresh_metadata()

    def find_campgrounds(self, *args, **kwargs):
        self.refresh_stale_metadata()
        return super().find_campgrounds(*args, **kwargs)

    def make_http_request(
        self,
        url,
        method="GET",
        data=None,
        headers=None,
        retry_response_codes=None,
    ):
        if retry_response_codes is None:
            retry_response_codes = self.FIVE_HUNDRED_STATUS_CODES
        with self.request_gate.slot(self.request_priority):
            response = self.session.request(
                method=method,
                url=url,
                data=data,
                headers=headers,
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code not in retry_response_codes:
                try:
                    response.raise_for_status()
                except Exception as exc:
                    # Pause before releasing the slot so batches already
                    # executing stop issuing requests immediately.
                    pause_gate_on_rate_limit(self.request_gate, exc, provider="ReserveCalifornia")
                    raise
            else:
                error_message = (
                    f"HTTP Error - {self.__class__.__name__} - "
                    f"{response.url} - {response.status_code}"
                )
                logger.warning(error_message)
                error_message += f": {response.text}"
                raise ProviderError(error_message)
        return response


@lru_cache(maxsize=None)
def provider_class_for_priority(priority: int) -> type:
    """Return a provider class that issues requests at ``priority``.

    The identity provider is built before any per-request argument is
    available, so the priority has to be carried by the class itself for the
    metadata lookups made during construction to be gated correctly.
    """
    if priority == PRIORITY_ALERT_REQUEST:
        return TimeoutReserveCalifornia
    return type(
        f"{TimeoutReserveCalifornia.__name__}Priority{priority}",
        (TimeoutReserveCalifornia,),
        {"request_priority": priority},
    )


def _as_list(value) -> list:
    if value in (None, (), []):
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _grid_window_starts(needed_days: set[date]) -> list[date]:
    """Fewest ``MAX_GRID_WINDOW_DAYS`` windows that cover every needed day."""
    starts: list[date] = []
    ordered = sorted(needed_days)
    index = 0
    while index < len(ordered):
        start = ordered[index]
        starts.append(start)
        cutoff = start + timedelta(days=MAX_GRID_WINDOW_DAYS - 1)
        while index < len(ordered) and ordered[index] <= cutoff:
            index += 1
    return starts


class NativeSearchReserveCalifornia:
    """Direct ReserveCalifornia availability search with bounded retries.

    Replaces camply's ``SearchReserveCalifornia``, which loses the tail of
    every month to the grid endpoint's 21-day response cap, retries server
    errors for up to 100 minutes with nothing above it imposing a deadline, and
    serialises all availability traffic through a process-global
    ``1 request/second`` decorator that the gate's alert-first ordering cannot
    see past.
    """

    provider_class = TimeoutReserveCalifornia
    provider_name = "ReserveCalifornia"

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
        provider: TimeoutReserveCalifornia | None = None,
        **_kwargs,
    ):
        if not any((campgrounds, recreation_area)):
            raise ValueError("A campground or recreation area ID is required")
        if nights < 1:
            raise ValueError("nights must be at least 1")

        self.search_window = _as_list(search_window)
        self.weekends_only = weekends_only
        self.nights = nights
        self.campsites = {str(identifier) for identifier in _as_list(campsites)}
        self.session = session or requests.Session()
        self._sleep = sleep
        self._request_gate = request_gate or RESERVE_CALIFORNIA_REQUEST_GATE
        self._request_metrics = request_metrics or PROVIDER_REQUEST_METRICS
        self.request_priority = request_priority
        self._max_rate_limit_retries = (
            ALERT_MAX_RATE_LIMIT_RETRIES
            if request_priority == PRIORITY_ALERT_REQUEST
            else DEFAULT_MAX_RATE_LIMIT_RETRIES
        )

        # `find_campgrounds` refreshes the offline metadata, which is what
        # populates the unit category/type names used to label results. The
        # provider is built from a priority-carrying class so those lookups are
        # gated at the priority of the scan that triggered them.
        provider = provider or provider_class_for_priority(request_priority)()
        self.campgrounds = provider.find_campgrounds(
            rec_area_id=_as_list(recreation_area),
            campground_id=_as_list(campgrounds),
            verbose=False,
        )
        self._provider = provider
        self._unit_categories = dict(provider.usedirect_unit_categories)
        self._unit_type_groups = dict(provider.usedirect_unit_type_groups)

    def _search_days(self) -> set[date]:
        """Days a stay may start on."""
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

    def _needed_days(self, search_days: set[date]) -> set[date]:
        """Days availability is needed for, including nights after a start.

        With ``nights > 1`` a stay runs past its start day, and with
        ``weekends_only`` those trailing nights are not themselves search days,
        so windows have to be sized against this set rather than the starts.
        """
        return {
            day + timedelta(days=offset) for day in search_days for offset in range(self.nights)
        }

    def _pause_gate_if_rate_limited(self, response) -> None:
        """Raise for error responses, deferring the shared gate on HTTP 429."""
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            pause_gate_on_rate_limit(self._request_gate, exc, provider="ReserveCalifornia")
            raise

    def _request_window(self, facility_id: int | str, start: date, end: date) -> dict:
        """Fetch one availability window, mirroring camply's request body."""
        payload = {
            "StartDate": start.strftime(UseDirectConfig.DATE_FORMAT),
            "EndDate": end.strftime(UseDirectConfig.DATE_FORMAT),
            "WebOnly": True,
            "UnitSort": "orderby",
            "InSeasonOnly": True,
            "FacilityId": facility_id,
        }
        body = json.dumps(payload)
        last_error: Exception | None = None
        rate_limit_retries = 0
        gate_paced = False

        for attempt in range(DEFAULT_MAX_ATTEMPTS):
            self._request_metrics.record_attempt(self.provider_name)
            try:
                with self._request_gate.slot(self.request_priority):
                    response = self.session.post(
                        GRID_URL,
                        data=body,
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
                gate_paced = False
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code == 429:
                    retryable = rate_limit_retries < self._max_rate_limit_retries
                    rate_limit_retries += 1
                    gate_paced = True
                else:
                    retryable = status_code is not None and 500 <= status_code < 600
                    gate_paced = False
            except (TypeError, ValueError) as exc:
                last_error = exc
                retryable = False
                gate_paced = False

            if not retryable or attempt == DEFAULT_MAX_ATTEMPTS - 1:
                self._request_metrics.record_failure(self.provider_name)
                raise last_error

            # The gate already holds the provider's own backoff, so sleeping a
            # local delay on top of it would just add latency.
            delay = 0.0 if gate_paced else DEFAULT_RETRY_DELAYS_SECONDS[attempt]
            self._request_metrics.record_retry(self.provider_name)
            logger.warning(
                "ReserveCalifornia request failed for facility %s (%s); retrying %s",
                facility_id,
                last_error,
                "once the provider pause elapses" if gate_paced else f"in {delay:.1f}s",
            )
            if delay:
                self._sleep(delay)

        raise RuntimeError("unreachable")

    def _make_result(
        self,
        *,
        campground: CampgroundFacility,
        unit: dict,
        booking_date: datetime,
        nights: int,
        location: CampsiteLocation | None,
    ) -> AvailableCampsite:
        return AvailableCampsite(
            campsite_id=unit.get("UnitId"),
            booking_date=booking_date,
            booking_end_date=booking_date + timedelta(days=nights),
            booking_nights=nights,
            campsite_site_name=unit.get("Name") or str(unit.get("UnitId")),
            campsite_loop_name=None,
            campsite_type=self._unit_categories.get(unit.get("UnitCategoryId", -1)),
            campsite_occupancy=(0, 1),
            campsite_use_type=self._unit_type_groups.get(unit.get("UnitTypeGroupId", -1)),
            availability_status="Available",
            recreation_area=campground.recreation_area or "",
            recreation_area_id=campground.recreation_area_id or 0,
            facility_name=campground.facility_name or "",
            facility_id=campground.facility_id,
            booking_url=self._provider.get_booking_url(
                recreation_area_id=campground.recreation_area_id,
                facility_id=campground.facility_id,
            ),
            permitted_equipment=[],
            campsite_attributes=[],
            location=location,
        )

    def _results_for_campground(
        self,
        campground: CampgroundFacility,
        search_days: set[date],
        needed_days: set[date],
    ) -> list[AvailableCampsite]:
        units_by_id: dict[str, dict] = {}
        available_by_unit: dict[str, set[date]] = {}
        location: CampsiteLocation | None = None

        for start in _grid_window_starts(needed_days):
            payload = self._request_window(
                campground.facility_id,
                start,
                start + timedelta(days=MAX_GRID_WINDOW_DAYS - 1),
            )
            facility = payload.get("Facility") or {}
            if location is None and facility.get("Latitude") and facility.get("Longitude"):
                location = CampsiteLocation(
                    latitude=facility["Latitude"],
                    longitude=facility["Longitude"],
                )
            for unit in (facility.get("Units") or {}).values():
                unit_id = str(unit.get("UnitId"))
                if self.campsites and unit_id not in self.campsites:
                    continue
                units_by_id[unit_id] = unit
                for availability in (unit.get("Slices") or {}).values():
                    if availability.get("IsFree") is not True:
                        continue
                    day = date.fromisoformat(availability["Date"])
                    # Windows can reach past the search window; ignoring the
                    # overhang keeps results independent of window alignment.
                    if day in needed_days:
                        available_by_unit.setdefault(unit_id, set()).add(day)

        results = []
        for unit_id, available_dates in available_by_unit.items():
            for day in sorted(available_dates & search_days):
                stay_dates = {day + timedelta(days=offset) for offset in range(self.nights)}
                if not stay_dates.issubset(available_dates):
                    continue
                results.append(
                    self._make_result(
                        campground=campground,
                        unit=units_by_id[unit_id],
                        booking_date=datetime.combine(day, datetime.min.time()),
                        nights=self.nights,
                        location=location,
                    )
                )
        return results

    def get_matching_campsites(
        self,
        log: bool = True,
        verbose: bool = False,
        continuous: bool = False,
        **_kwargs,
    ) -> list[AvailableCampsite]:
        if continuous:
            raise ValueError("Native ReserveCalifornia search only supports one-shot scans")
        if not self.campgrounds:
            raise RuntimeError("No campgrounds found to search")

        search_days = self._search_days()
        if not search_days:
            return []
        needed_days = self._needed_days(search_days)

        results = []
        for campground in self.campgrounds:
            results.extend(self._results_for_campground(campground, search_days, needed_days))
        if log:
            logger.info(
                "%d ReserveCalifornia campsite-date combinations matched the search",
                len(results),
            )
        return results
