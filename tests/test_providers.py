"""Tests for the campsite_checker.providers registry.

Per-provider behavior lives in test_recreation_gov.py and
test_reserve_california.py; this file only covers the name → class wiring.
"""

from campsite_checker.providers import (
    METADATA_PROVIDER_CLASS,
    PROVIDER_DISPLAY,
    PROVIDER_MAP,
)
from campsite_checker.providers.recreation_gov import (
    IdentityCachedRecreationDotGov,
    NativeSearchRecreationDotGov,
)
from campsite_checker.providers.reserve_america import NativeSearchReserveAmerica
from campsite_checker.providers.reserve_california import (
    NativeSearchReserveCalifornia,
    TimeoutReserveCalifornia,
)


class TestProviderMap:
    def test_has_expected_providers(self):
        assert "RecreationDotGov" in PROVIDER_MAP
        assert "Yellowstone" in PROVIDER_MAP
        assert "GoingToCamp" in PROVIDER_MAP
        assert "ReserveCalifornia" in PROVIDER_MAP
        assert "ReserveAmerica" in PROVIDER_MAP

    def test_all_providers_have_display_names(self):
        for key in PROVIDER_MAP:
            assert key in PROVIDER_DISPLAY

    def test_all_display_names_have_providers(self):
        for key in PROVIDER_DISPLAY:
            assert key in PROVIDER_MAP


class TestProviderMapWiring:
    def test_recreation_dot_gov_uses_native_search_class(self):
        from camply.search import SearchRecreationDotGov

        assert PROVIDER_MAP["RecreationDotGov"] is NativeSearchRecreationDotGov
        assert not issubclass(NativeSearchRecreationDotGov, SearchRecreationDotGov)
        assert NativeSearchRecreationDotGov.provider_class is IdentityCachedRecreationDotGov

    def test_reserve_california_uses_native_search_class(self):
        from camply.search import SearchReserveCalifornia

        assert PROVIDER_MAP["ReserveCalifornia"] is NativeSearchReserveCalifornia
        assert not issubclass(NativeSearchReserveCalifornia, SearchReserveCalifornia)
        assert NativeSearchReserveCalifornia.provider_class is TimeoutReserveCalifornia

    def test_reserve_america_uses_native_search_class(self):
        assert PROVIDER_MAP["ReserveAmerica"] is NativeSearchReserveAmerica

    def test_metadata_provider_classes_share_hardening(self):
        assert METADATA_PROVIDER_CLASS["RecreationDotGov"] is IdentityCachedRecreationDotGov
        assert METADATA_PROVIDER_CLASS["ReserveCalifornia"] is TimeoutReserveCalifornia
