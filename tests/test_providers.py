"""Tests for campsite_checker.providers."""

from campsite_checker.providers import PROVIDER_DISPLAY, PROVIDER_MAP, WEEKDAY_NAMES


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
