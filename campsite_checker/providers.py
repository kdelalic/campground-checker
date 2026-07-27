import logging
import os
import pathlib
import threading
import time
from collections import OrderedDict

from camply.containers import CampgroundFacility
from camply.providers import RecreationDotGov
from camply.providers.base_provider import ProviderError
from camply.providers.usedirect.variations import ReserveCalifornia
from camply.search import (
    SearchGoingToCamp,
    SearchRecreationDotGov,
    SearchReserveCalifornia,
    SearchYellowstone,
)

logger = logging.getLogger(__name__)

# Applied to providers whose stock camply HTTP calls carry no timeout; a
# black-holed connection would otherwise hang a scan thread forever.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30

# Camply's UseDirect providers default their offline metadata cache to a
# directory beside the installed package, which is read-only in the container
# (the process runs as an unprivileged user). The cache must live somewhere
# writable; the container sets CAMPLY_CACHE_DIR to the persistent state mount.
CAMPLY_CACHE_DIR_ENV = "CAMPLY_CACHE_DIR"
DEFAULT_CAMPLY_CACHE_DIR = pathlib.Path(".camply-cache")


def usedirect_cache_dir(provider_slug: str) -> pathlib.Path:
    """Writable offline-cache directory for a UseDirect provider."""
    base = os.environ.get(CAMPLY_CACHE_DIR_ENV)
    root = pathlib.Path(base) if base else DEFAULT_CAMPLY_CACHE_DIR
    return root / provider_slug


class FacilityIdentityCache:
    """Thread-safe bounded TTL cache for resolved facility identity records."""

    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: float = 24 * 60 * 60,
        clock=time.monotonic,
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
    """RecreationDotGov provider that caches facility identity lookups.

    Camply resolves each configured campground ID to its facility name and
    recreation area with one serial RIDB round trip (~1s) during every
    searcher construction. That identity data is immutable in practice, so
    resolved ``CampgroundFacility`` records are shared process-wide for a
    day. Failed or filtered lookups are never cached, so transient RIDB
    errors are retried on the next scan and stay distinguishable from
    genuine results.
    """

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


class IdentityCachedSearchRecreationDotGov(SearchRecreationDotGov):
    """SearchRecreationDotGov wired to the identity-caching provider."""

    provider_class = IdentityCachedRecreationDotGov


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
    """

    @property
    def offline_cache_dir(self) -> pathlib.Path:
        return usedirect_cache_dir("reserve-california")

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
        response = self.session.request(
            method=method,
            url=url,
            data=data,
            headers=headers,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code not in retry_response_codes:
            response.raise_for_status()
        else:
            error_message = (
                f"HTTP Error - {self.__class__.__name__} - {response.url} - {response.status_code}"
            )
            logger.warning(error_message)
            error_message += f": {response.text}"
            raise ProviderError(error_message)
        return response


class TimeoutSearchReserveCalifornia(SearchReserveCalifornia):
    """SearchReserveCalifornia wired to the timeout-enforcing provider."""

    provider_class = TimeoutReserveCalifornia


PROVIDER_MAP: dict[str, type] = {
    "RecreationDotGov": IdentityCachedSearchRecreationDotGov,
    "Yellowstone": SearchYellowstone,
    "GoingToCamp": SearchGoingToCamp,
    "ReserveCalifornia": TimeoutSearchReserveCalifornia,
}

# Provider classes used for out-of-search metadata lookups (bot name resolution),
# sharing the same caching/timeout hardening as the search path.
METADATA_PROVIDER_CLASS: dict[str, type] = {
    "RecreationDotGov": IdentityCachedRecreationDotGov,
    "ReserveCalifornia": TimeoutReserveCalifornia,
}

PROVIDER_DISPLAY: dict[str, str] = {
    "RecreationDotGov": "recreation.gov",
    "Yellowstone": "yellowstone",
    "GoingToCamp": "goingtocamping.com",
    "ReserveCalifornia": "reservecalifornia",
}

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

WEEKDAY_LABELS = {value: name.capitalize() for name, value in WEEKDAY_NAMES.items()}
