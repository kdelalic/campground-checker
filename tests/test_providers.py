"""Tests for campsite_checker.providers."""

from datetime import date, timedelta

import pytest
from camply.containers import CampgroundFacility, SearchWindow
from camply.providers import RecreationDotGov

from campsite_checker.providers import (
    PROVIDER_DISPLAY,
    PROVIDER_MAP,
    WEEKDAY_LABELS,
    WEEKDAY_NAMES,
)
from campsite_checker.recreation_gov import (
    FACILITY_IDENTITY_CACHE,
    FacilityIdentityCache,
    IdentityCachedRecreationDotGov,
    NativeSearchRecreationDotGov,
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
        first = NativeSearchRecreationDotGov(search_window=window, campgrounds=[232491], nights=1)
        second = NativeSearchRecreationDotGov(search_window=window, campgrounds=[232491], nights=1)
        assert calls == [[232491]]
        assert first.campgrounds == [facilities["232491"]]
        assert second.campgrounds == first.campgrounds


class TestProviderMapWiring:
    def test_recreation_dot_gov_uses_native_search_class(self):
        from camply.search import SearchRecreationDotGov

        assert PROVIDER_MAP["RecreationDotGov"] is NativeSearchRecreationDotGov
        assert not issubclass(NativeSearchRecreationDotGov, SearchRecreationDotGov)
        assert NativeSearchRecreationDotGov.provider_class is IdentityCachedRecreationDotGov


class TestTimeoutReserveCalifornia:
    def test_provider_map_uses_timeout_search_class(self):
        from camply.search import SearchReserveCalifornia

        from campsite_checker.providers import (
            TimeoutReserveCalifornia,
            TimeoutSearchReserveCalifornia,
        )

        assert PROVIDER_MAP["ReserveCalifornia"] is TimeoutSearchReserveCalifornia
        assert issubclass(TimeoutSearchReserveCalifornia, SearchReserveCalifornia)
        assert TimeoutSearchReserveCalifornia.provider_class is TimeoutReserveCalifornia

    def test_make_http_request_passes_timeout(self):
        """Stock camply UseDirect requests hang forever without a timeout."""
        from types import SimpleNamespace

        from campsite_checker.providers import (
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            TimeoutReserveCalifornia,
        )

        captured = {}

        def fake_request(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                url=kwargs["url"],
                text="",
            )

        fake_self = SimpleNamespace(
            session=SimpleNamespace(request=fake_request),
            FIVE_HUNDRED_STATUS_CODES=[500, 502, 503],
        )
        response = TimeoutReserveCalifornia.make_http_request(fake_self, "https://example.com/api")

        assert response.status_code == 200
        assert captured["timeout"] == DEFAULT_HTTP_TIMEOUT_SECONDS

    def test_offline_cache_dir_defaults_outside_install_tree(self, monkeypatch):
        """Camply defaults the UseDirect cache to site-packages, which the
        unprivileged container user cannot write; the override must land
        somewhere else."""
        import pathlib

        import camply

        from campsite_checker.providers import (
            CAMPLY_CACHE_DIR_ENV,
            DEFAULT_CAMPLY_CACHE_DIR,
            TimeoutReserveCalifornia,
        )

        monkeypatch.delenv(CAMPLY_CACHE_DIR_ENV, raising=False)
        cache_dir = TimeoutReserveCalifornia().offline_cache_dir
        assert cache_dir == DEFAULT_CAMPLY_CACHE_DIR / "reserve-california"
        camply_install_tree = pathlib.Path(camply.__file__).parent
        assert camply_install_tree not in cache_dir.resolve().parents

    def test_offline_cache_dir_honors_env_override(self, monkeypatch, tmp_path):
        from campsite_checker.providers import (
            CAMPLY_CACHE_DIR_ENV,
            TimeoutReserveCalifornia,
        )

        monkeypatch.setenv(CAMPLY_CACHE_DIR_ENV, str(tmp_path / "camply-cache"))
        cache_dir = TimeoutReserveCalifornia().offline_cache_dir
        assert cache_dir == tmp_path / "camply-cache" / "reserve-california"

    def test_metadata_provider_classes_share_hardening(self):
        from campsite_checker.providers import (
            METADATA_PROVIDER_CLASS,
            TimeoutReserveCalifornia,
        )

        assert METADATA_PROVIDER_CLASS["RecreationDotGov"] is IdentityCachedRecreationDotGov
        assert METADATA_PROVIDER_CLASS["ReserveCalifornia"] is TimeoutReserveCalifornia
