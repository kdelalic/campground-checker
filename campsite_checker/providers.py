import threading
import time
from collections import OrderedDict

from camply.containers import CampgroundFacility
from camply.providers import RecreationDotGov
from camply.search import (
    SearchGoingToCamp,
    SearchRecreationDotGov,
    SearchReserveCalifornia,
    SearchYellowstone,
)


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


PROVIDER_MAP: dict[str, type] = {
    "RecreationDotGov": IdentityCachedSearchRecreationDotGov,
    "Yellowstone": SearchYellowstone,
    "GoingToCamp": SearchGoingToCamp,
    "ReserveCalifornia": SearchReserveCalifornia,
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
