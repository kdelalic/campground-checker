"""Tests for campsite_checker.weekdays."""

from campsite_checker.weekdays import WEEKDAY_LABELS, WEEKDAY_NAMES


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
