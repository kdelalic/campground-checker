"""Tests for campsite_checker.providers."""

from datetime import date, timedelta

import pytest
from camply.containers import CampgroundFacility, SearchWindow
from camply.providers import RecreationDotGov

from campsite_checker.providers import (
    FACILITY_IDENTITY_CACHE,
    PROVIDER_DISPLAY,
    PROVIDER_MAP,
    WEEKDAY_LABELS,
    WEEKDAY_NAMES,
    FacilityIdentityCache,
    IdentityCachedRecreationDotGov,
    IdentityCachedSearchRecreationDotGov,
)


def make_facility(facility_id, name="Kirby Cove", area="Golden Gate"):
    return CampgroundFacility(
        facility_name=name,
        recreation_area=area,
        facility_id=facility_id,
        recreation_area_id=1000,
        map_id=None,
        coordinates=None,
    )


@pytest.fixture(autouse=True)
def clear_identity_cache():
    FACILITY_IDENTITY_CACHE.clear()
    yield
    FACILITY_IDENTITY_CACHE.clear()


class TestProviderMap:
    def test_has_expected_providers(self):
        assert "RecreationDotGov" in PROVIDER_MAP
        assert "Yellowstone" in PROVIDER_MAP
        assert "GoingToCamp" in PROVIDER_MAP
        assert "ReserveCalifornia" in PROVIDER_MAP

    def test_all_providers_have_display_names(self):
        for key in PROVIDER_MAP:
            assert key in PROVIDER_DISPLAY

    def test_all_display_names_have_providers(self):
        for key in PROVIDER_DISPLAY:
            assert key in PROVIDER_MAP


class TestWeekdayNames:
    def test_all_seven_days(self):
        assert len(WEEKDAY_NAMES) == 7

    def test_monday_is_zero(self):
        assert WEEKDAY_NAMES["monday"] == 0

    def test_sunday_is_six(self):
        assert WEEKDAY_NAMES["sunday"] == 6

    def test_all_lowercase(self):
        for key in WEEKDAY_NAMES:
            assert key == key.lower()

    def test_values_are_unique(self):
        values = list(WEEKDAY_NAMES.values())
        assert len(values) == len(set(values))

    def test_precomputed_labels_match_names(self):
        assert WEEKDAY_LABELS[0] == "Monday"
        assert WEEKDAY_LABELS[6] == "Sunday"


class TestFacilityIdentityCache:
    def test_store_and_get_roundtrip(self):
        cache = FacilityIdentityCache()
        facility = make_facility(232491)
        cache.store("232491", facility)
        assert cache.get("232491") is facility
        assert len(cache) == 1

    def test_get_missing_returns_none(self):
        cache = FacilityIdentityCache()
        assert cache.get("999") is None

    def test_entries_expire_after_ttl(self):
        now = [0.0]
        cache = FacilityIdentityCache(ttl_seconds=100, clock=lambda: now[0])
        cache.store("1", make_facility(1))
        now[0] = 99.0
        assert cache.get("1") is not None
        now[0] = 101.0
        assert cache.get("1") is None
        assert len(cache) == 0

    def test_eviction_keeps_most_recently_used(self):
        cache = FacilityIdentityCache(max_entries=2)
        cache.store("1", make_facility(1))
        cache.store("2", make_facility(2))
        cache.get("1")
        cache.store("3", make_facility(3))
        assert cache.get("1") is not None
        assert cache.get("2") is None
        assert cache.get("3") is not None

    def test_clear_empties_cache(self):
        cache = FacilityIdentityCache()
        cache.store("1", make_facility(1))
        cache.clear()
        assert len(cache) == 0


class TestIdentityCachedRecreationDotGov:
    @pytest.fixture
    def parent_lookup(self, monkeypatch):
        """Stub the camply RIDB lookup and record every call."""
        calls = []
        facilities = {
            "232491": make_facility(232491, name="Kirby Cove"),
            "232447": make_facility(232447, name="Upper Pines", area="Yosemite"),
        }

        def fake_lookup(self, campground_id):
            calls.append(list(campground_id))
            return [
                facilities[str(identifier)]
                for identifier in campground_id
                if str(identifier) in facilities
            ]

        monkeypatch.setattr(
            RecreationDotGov,
            "_find_facilities_from_campgrounds",
            fake_lookup,
        )
        return calls, facilities

    def test_first_lookup_fetches_and_caches(self, parent_lookup):
        calls, facilities = parent_lookup
        provider = IdentityCachedRecreationDotGov()
        result = provider._find_facilities_from_campgrounds([232491])
        assert result == [facilities["232491"]]
        assert calls == [[232491]]
        assert FACILITY_IDENTITY_CACHE.get("232491") is facilities["232491"]

    def test_second_lookup_skips_network(self, parent_lookup):
        calls, facilities = parent_lookup
        first = IdentityCachedRecreationDotGov()
        second = IdentityCachedRecreationDotGov()
        first._find_facilities_from_campgrounds([232491])
        result = second._find_facilities_from_campgrounds([232491])
        assert result == [facilities["232491"]]
        assert calls == [[232491]]

    def test_partial_hit_fetches_only_misses_and_preserves_order(self, parent_lookup):
        calls, facilities = parent_lookup
        provider = IdentityCachedRecreationDotGov()
        FACILITY_IDENTITY_CACHE.store("232447", facilities["232447"])
        result = provider._find_facilities_from_campgrounds([232491, 232447])
        assert calls == [[232491]]
        assert result == [facilities["232491"], facilities["232447"]]

    def test_unresolvable_ids_are_omitted_and_not_cached(self, parent_lookup):
        calls, _facilities = parent_lookup
        provider = IdentityCachedRecreationDotGov()
        result = provider._find_facilities_from_campgrounds([999])
        assert result == []
        assert FACILITY_IDENTITY_CACHE.get("999") is None
        provider._find_facilities_from_campgrounds([999])
        assert calls == [[999], [999]]

    def test_lookup_failure_propagates_and_caches_nothing(self, monkeypatch):
        def failing_lookup(self, campground_id):
            raise ConnectionError("RIDB unavailable")

        monkeypatch.setattr(
            RecreationDotGov,
            "_find_facilities_from_campgrounds",
            failing_lookup,
        )
        provider = IdentityCachedRecreationDotGov()
        with pytest.raises(ConnectionError):
            provider._find_facilities_from_campgrounds([232491])
        assert len(FACILITY_IDENTITY_CACHE) == 0

    def test_searcher_construction_reuses_identity(self, parent_lookup):
        calls, facilities = parent_lookup
        start = date.today() + timedelta(days=30)
        window = SearchWindow(start_date=start, end_date=start + timedelta(days=2))
        first = IdentityCachedSearchRecreationDotGov(
            search_window=window, campgrounds=[232491], nights=1
        )
        second = IdentityCachedSearchRecreationDotGov(
            search_window=window, campgrounds=[232491], nights=1
        )
        assert calls == [[232491]]
        assert first.campgrounds == [facilities["232491"]]
        assert second.campgrounds == first.campgrounds


class TestProviderMapWiring:
    def test_recreation_dot_gov_uses_identity_cached_search_class(self):
        from camply.search import SearchRecreationDotGov

        assert PROVIDER_MAP["RecreationDotGov"] is IdentityCachedSearchRecreationDotGov
        assert issubclass(IdentityCachedSearchRecreationDotGov, SearchRecreationDotGov)
        assert IdentityCachedSearchRecreationDotGov.provider_class is IdentityCachedRecreationDotGov
