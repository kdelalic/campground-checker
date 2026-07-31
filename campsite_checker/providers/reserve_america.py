"""ReserveAmerica availability search using its public campground pages.

ReserveAmerica's documented campground API exposes metadata but not live
inventory.  The public campsite-availability page server-renders the same
14-day, per-site grid shown to visitors into an ``initialState`` JSON script.
This client reads that state without depending on browser JavaScript or a
private API credential.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Callable

import requests
from camply.containers import AvailableCampsite, CampgroundFacility, SearchWindow
from camply.containers.data_containers import CampsiteLocation

from ..request_gate import (
    PRIORITY_ALERT_REQUEST,
    PROVIDER_REQUEST_METRICS,
    ProviderRequestMetrics,
    RequestGate,
    pause_gate_on_rate_limit,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.reserveamerica.com"
AVAILABILITY_PATH = "/explore/campground/{contract_code}/{facility_id}/campsite-availability"
BOOKING_PATH = "/explore/campground/{contract_code}/{facility_id}/{product_id}/campsite-booking"

# The rendered product grid contains 14 days and ReserveAmerica caps result
# pages at 50 products even when a larger page size is requested.
GRID_DAYS = 14
RECORDS_PER_PAGE = 50
MAX_RESULT_PAGES = 20
AVAILABLE_STATUSES = frozenset({"AVAILABLE", "AVAILABLE_BEYOUND_STAY"})

DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_READ_TIMEOUT_SECONDS = 20
DEFAULT_REQUEST_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAYS_SECONDS = (1.0, 2.0)

# The public page is substantially larger than the JSON endpoints used by the
# other providers. Keep its starts serial and gently paced.
RESERVE_AMERICA_REQUEST_GATE = RequestGate(max_concurrent=1, requests_per_second=1)

REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (compatible; campground-checker/1.0; "
        "+https://github.com/kdelalic/campground-checker)"
    ),
}


class ReserveAmericaResponseError(ValueError):
    """The public page did not contain a usable availability state."""


class _InitialStateParser(HTMLParser):
    """Extract the JSON script without retaining unrelated page markup."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._capturing = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "initialState":
            self._capturing = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False

    @property
    def content(self) -> str:
        return "".join(self._chunks)


def parse_availability_page(page_html: str) -> tuple[dict, dict]:
    """Return ``(facility, product search results)`` from a rendered page."""
    parser = _InitialStateParser()
    parser.feed(page_html)
    if not parser.content:
        raise ReserveAmericaResponseError("ReserveAmerica page omitted initial availability state")

    try:
        state = json.loads(parser.content)
        backend = state["backend"]
        facility = backend["facility"]["facility"]
        search_results = backend["productSearch"]["searchResults"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReserveAmericaResponseError(
            "ReserveAmerica page contained invalid availability state"
        ) from exc

    if not isinstance(facility, dict) or not isinstance(search_results, dict):
        raise ReserveAmericaResponseError("ReserveAmerica page contained invalid campground data")
    records = search_results.get("records")
    if not isinstance(records, list):
        raise ReserveAmericaResponseError(
            "ReserveAmerica page omitted campsite availability records"
        )
    return facility, search_results


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _attribute_value(details: dict, attribute_id: int) -> str | None:
    for attribute in details.get("attributes") or []:
        if attribute.get("id") != attribute_id:
            continue
        values = attribute.get("displayValue") or attribute.get("value") or []
        if values:
            return str(values[0])
    return None


def _site_location(record: dict, facility: dict) -> CampsiteLocation | None:
    details = record.get("details") or {}
    latitude = _attribute_value(details, 11059)
    longitude = _attribute_value(details, 11060)
    if latitude is None or longitude is None:
        coordinates = facility.get("coordinates") or {}
        latitude = coordinates.get("latitude")
        longitude = coordinates.get("longitude")
    try:
        return CampsiteLocation(latitude=float(latitude), longitude=float(longitude))
    except (TypeError, ValueError):
        return None


class NativeSearchReserveAmerica:
    """Search ReserveAmerica's public, server-rendered availability grid."""

    provider_name = "ReserveAmerica"

    def __init__(
        self,
        search_window: SearchWindow | list[SearchWindow],
        recreation_area: list[int] | int | None = None,
        campgrounds: list[int] | int | None = None,
        campsites: list[int] | int | None = None,
        weekends_only: bool = False,
        nights: int = 1,
        *,
        contract_code: str,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_gate: RequestGate | None = None,
        request_metrics: ProviderRequestMetrics | None = None,
        request_priority: int = PRIORITY_ALERT_REQUEST,
        **_kwargs,
    ):
        facility_ids = _as_list(campgrounds)
        if not facility_ids:
            raise ValueError("A ReserveAmerica campground ID is required")
        if recreation_area:
            raise ValueError("ReserveAmerica does not support recreation_area entries")
        if not isinstance(contract_code, str) or not contract_code.strip():
            raise ValueError("A ReserveAmerica contract_code is required")
        if nights < 1:
            raise ValueError("nights must be at least 1")

        self.search_window = _as_list(search_window)
        self.facility_ids = facility_ids
        self.contract_code = contract_code.strip().upper()
        self.campsites = {str(identifier) for identifier in _as_list(campsites)}
        self.weekends_only = weekends_only
        self.nights = nights
        self.session = session or requests.Session()
        self._sleep = sleep
        self._request_gate = request_gate or RESERVE_AMERICA_REQUEST_GATE
        self._request_metrics = request_metrics or PROVIDER_REQUEST_METRICS
        self.request_priority = request_priority

        # Search orchestration introspects this attribute for names before the
        # first request. Config comments remain authoritative for empty scans;
        # successful results carry the provider's live facility name.
        self.campgrounds = [
            CampgroundFacility(
                facility_name=f"ReserveAmerica campground {facility_id}",
                recreation_area="",
                facility_id=facility_id,
                recreation_area_id=self.contract_code,
                map_id=None,
                coordinates=None,
            )
            for facility_id in self.facility_ids
        ]

    def _search_days(self) -> set[date]:
        today = date.today()
        allowed_weekdays = {4, 5} if self.weekends_only else set(range(7))
        days: set[date] = set()
        for window in self.search_window:
            current = max(window.start_date, today)
            while current < window.end_date:
                if current.weekday() in allowed_weekdays:
                    days.add(current)
                current += timedelta(days=1)
        return days

    def _needed_days(self, search_days: set[date]) -> set[date]:
        return {
            day + timedelta(days=offset) for day in search_days for offset in range(self.nights)
        }

    def _window_starts(self, needed_days: set[date]):
        current = min(needed_days)
        final = max(needed_days)
        while current <= final:
            yield current
            current += timedelta(days=GRID_DAYS)

    def _request_page(
        self,
        facility_id: int | str,
        start: date,
        page_number: int,
    ) -> tuple[dict, dict]:
        path = AVAILABILITY_PATH.format(
            contract_code=self.contract_code,
            facility_id=facility_id,
        )
        url = f"{BASE_URL}{path}"
        params = {
            "availStartDate": start.isoformat(),
            "pageNumber": page_number,
            "recordsPerPage": RECORDS_PER_PAGE,
            "nextAvailableDate": "false",
        }
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
                    try:
                        response.raise_for_status()
                    except requests.HTTPError as exc:
                        pause_gate_on_rate_limit(
                            self._request_gate,
                            exc,
                            provider=self.provider_name,
                        )
                        raise
                return parse_availability_page(response.text)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                retryable = True
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                retryable = status_code == 429 or (
                    status_code is not None and 500 <= status_code < 600
                )
            except ReserveAmericaResponseError as exc:
                last_error = exc
                retryable = True

            if not retryable or attempt == DEFAULT_MAX_ATTEMPTS - 1:
                self._request_metrics.record_failure(self.provider_name)
                raise last_error

            delay = DEFAULT_RETRY_DELAYS_SECONDS[attempt]
            self._request_metrics.record_retry(self.provider_name)
            logger.warning(
                "ReserveAmerica request failed for facility %s page %s (%s); retrying in %.1fs",
                facility_id,
                page_number,
                last_error,
                delay,
            )
            self._sleep(delay)

        raise RuntimeError("unreachable")

    def _request_window(
        self,
        facility_id: int | str,
        start: date,
    ) -> tuple[dict, list[dict]]:
        facility, first_page = self._request_page(facility_id, start, 0)
        try:
            total_pages = int(first_page.get("totalPages", 1))
        except (TypeError, ValueError) as exc:
            raise ReserveAmericaResponseError(
                "ReserveAmerica returned invalid result pagination"
            ) from exc
        if total_pages < 0 or total_pages > MAX_RESULT_PAGES:
            raise ReserveAmericaResponseError(
                f"ReserveAmerica returned unsupported result page count: {total_pages}"
            )

        records = list(first_page["records"])
        for page_number in range(1, total_pages):
            page_facility, page = self._request_page(facility_id, start, page_number)
            if page_facility.get("id") != facility.get("id"):
                raise ReserveAmericaResponseError(
                    "ReserveAmerica pagination returned a different campground"
                )
            records.extend(page["records"])
        return facility, records

    def _booking_url(
        self,
        facility_id: int | str,
        product_id: int | str,
        booking_date: date,
    ) -> str:
        path = BOOKING_PATH.format(
            contract_code=self.contract_code,
            facility_id=facility_id,
            product_id=product_id,
        )
        return f"{BASE_URL}{path}?availStartDate={booking_date.isoformat()}&nextAvailableDate=false"

    def _make_result(
        self,
        *,
        facility: dict,
        record: dict,
        booking_date: date,
    ) -> AvailableCampsite:
        details = record.get("details") or {}
        minimum_occupancy = _attribute_value(details, 111)
        maximum_occupancy = _attribute_value(details, 12)
        try:
            occupancy = (int(minimum_occupancy or 0), int(maximum_occupancy or 1))
        except ValueError:
            occupancy = (0, 1)

        product_id = record["id"]
        facility_id = facility["id"]
        start = datetime.combine(booking_date, datetime.min.time())
        return AvailableCampsite(
            campsite_id=product_id,
            booking_date=start,
            booking_end_date=start + timedelta(days=self.nights),
            booking_nights=self.nights,
            campsite_site_name=record.get("name") or str(product_id),
            campsite_loop_name=details.get("loopName"),
            campsite_type=record.get("prodGrpName"),
            campsite_occupancy=occupancy,
            campsite_use_type=(record.get("prodInfo") or {}).get("typeOfUseLabel"),
            availability_status="Available",
            recreation_area="",
            recreation_area_id=self.contract_code,
            facility_name=facility.get("name") or f"Campground {facility_id}",
            facility_id=facility_id,
            booking_url=self._booking_url(facility_id, product_id, booking_date),
            permitted_equipment=[],
            campsite_attributes=[],
            location=_site_location(record, facility),
        )

    def _results_for_facility(
        self,
        facility_id: int | str,
        search_days: set[date],
        needed_days: set[date],
    ) -> list[AvailableCampsite]:
        facility: dict | None = None
        records_by_id: dict[str, dict] = {}
        available_by_product: dict[str, set[date]] = {}

        for start in self._window_starts(needed_days):
            window_facility, records = self._request_window(facility_id, start)
            facility = facility or window_facility
            for record in records:
                product_id = str(record.get("id"))
                if self.campsites and product_id not in self.campsites:
                    continue
                records_by_id[product_id] = record
                for availability in record.get("availabilityGrid") or []:
                    if availability.get("status") not in AVAILABLE_STATUSES:
                        continue
                    try:
                        available_date = date.fromisoformat(availability["date"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if available_date in needed_days:
                        available_by_product.setdefault(product_id, set()).add(available_date)

        if facility is None:
            raise ReserveAmericaResponseError(
                f"ReserveAmerica returned no campground data for {facility_id}"
            )

        results = []
        for product_id, available_dates in available_by_product.items():
            for day in sorted(available_dates & search_days):
                stay_dates = {day + timedelta(days=offset) for offset in range(self.nights)}
                if stay_dates.issubset(available_dates):
                    results.append(
                        self._make_result(
                            facility=facility,
                            record=records_by_id[product_id],
                            booking_date=day,
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
            raise ValueError("ReserveAmerica search only supports one-shot scans")

        search_days = self._search_days()
        if not search_days:
            return []
        needed_days = self._needed_days(search_days)

        results = []
        for facility_id in self.facility_ids:
            results.extend(self._results_for_facility(facility_id, search_days, needed_days))
        if log:
            logger.info(
                "%d ReserveAmerica campsite-date combinations matched the search",
                len(results),
            )
        return results
