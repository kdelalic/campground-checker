"""Tests for campsite_checker.results."""

from datetime import date, datetime
from types import SimpleNamespace

from campsite_checker.results import (
    count_matching_dates,
    filter_results,
    format_results,
    get_booking_url,
    get_facility_name,
    group_results,
    process_results,
)

from .conftest import make_campsite

# ── count_matching_dates ─────────────────────────────────────────────────────


class TestCountMatchingDates:
    def test_no_filter_counts_all_days(self):
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 8)
        assert count_matching_dates(start, end, None) == 7

    def test_saturday_filter(self):
        # July 2026: Saturdays are July 4 and July 11
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 15)
        assert count_matching_dates(start, end, {5}) == 2

    def test_single_day_range(self):
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 1)
        assert count_matching_dates(start, end, None) == 0

    def test_weekend_filter(self):
        # July 2026: July 4 (Sat), July 5 (Sun), July 11 (Sat), July 12 (Sun)
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 15)
        assert count_matching_dates(start, end, {5, 6}) == 4


# ── get_facility_name ──────────────────────────────────────────────────────


class TestGetFacilityName:
    def test_full_name(self):
        results = [make_campsite(recreation_area="Yosemite", facility_name="Upper Pines")]
        assert get_facility_name(results) == "Yosemite — Upper Pines"

    def test_only_recreation_area(self):
        results = [make_campsite(recreation_area="Yosemite", facility_name="")]
        assert get_facility_name(results) == "Yosemite"

    def test_only_facility(self):
        results = [make_campsite(recreation_area="", facility_name="Upper Pines")]
        assert get_facility_name(results) == "Upper Pines"

    def test_empty_results(self):
        assert get_facility_name([]) == "Unknown"

    def test_uses_first_result_with_name(self):
        results = [
            make_campsite(recreation_area="", facility_name=""),
            make_campsite(recreation_area="Yosemite", facility_name="Upper Pines"),
        ]
        assert get_facility_name(results) == "Yosemite — Upper Pines"


# ── get_booking_url ──────────────────────────────────────────────────────────


class TestGetBookingUrl:
    def test_url_found(self):
        results = [make_campsite(booking_url="https://example.com/book")]
        assert get_booking_url(results) == "https://example.com/book"

    def test_strips_web_default_fragment(self):
        results = [make_campsite(booking_url="https://example.com/Web/Default.aspx#!page")]
        assert get_booking_url(results) == "https://example.com/page"

    def test_empty_url(self):
        results = [make_campsite(booking_url="")]
        assert get_booking_url(results) == ""

    def test_no_results(self):
        assert get_booking_url([]) == ""


# ── filter_results ──────────────────────────────────────────────────────────


class TestFilterResults:
    def test_keeps_normal_sites(self):
        results = [make_campsite(campsite_type="STANDARD")]
        assert len(filter_results(results, None)) == 1

    def test_excludes_boat_in_by_type(self):
        results = [make_campsite(campsite_type="BOAT-IN")]
        assert len(filter_results(results, None)) == 0

    def test_excludes_hike_in_by_type(self):
        results = [make_campsite(campsite_type="HIKE-IN")]
        assert len(filter_results(results, None)) == 0

    def test_excludes_boat_in_by_site_name(self):
        results = [make_campsite(campsite_site_name="Boat Access Site")]
        assert len(filter_results(results, None)) == 0

    def test_excludes_hike_in_by_loop_name(self):
        results = [make_campsite(campsite_loop_name="Hike Loop")]
        assert len(filter_results(results, None)) == 0

    def test_excludes_by_site_access_attribute(self):
        attrs = [SimpleNamespace(attribute_name="Site Access", attribute_value="Hike-in")]
        results = [make_campsite(campsite_attributes=attrs)]
        assert len(filter_results(results, None)) == 0

    def test_day_filter_keeps_matching(self):
        # July 4 2026 is a Saturday (weekday=5)
        results = [make_campsite(booking_date=datetime(2026, 7, 4))]
        assert len(filter_results(results, {5})) == 1

    def test_day_filter_excludes_non_matching(self):
        # July 6 2026 is a Monday (weekday=0)
        results = [make_campsite(booking_date=datetime(2026, 7, 6))]
        assert len(filter_results(results, {5})) == 0

    def test_no_day_filter_keeps_all(self):
        results = [
            make_campsite(booking_date=datetime(2026, 7, 4)),
            make_campsite(booking_date=datetime(2026, 7, 6)),
        ]
        assert len(filter_results(results, None)) == 2


# ── group_results ───────────────────────────────────────────────────────────


class TestGroupResults:
    def test_groups_by_date(self):
        results = [
            make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4)),
            make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 4)),
            make_campsite(campsite_id=3, booking_date=datetime(2026, 7, 11)),
        ]
        grouped = group_results(results, None)
        assert grouped is not None
        name, by_date, total, url = grouped
        assert total == 3
        assert len(by_date[date(2026, 7, 4)]) == 2
        assert len(by_date[date(2026, 7, 11)]) == 1

    def test_none_for_no_results(self):
        assert group_results([], None) is None

    def test_filtered_out_returns_none(self):
        results = [make_campsite(campsite_type="BOAT-IN")]
        assert group_results(results, None) is None


class TestProcessedAvailability:
    def test_filters_deduplicates_and_groups_once(self):
        results = [
            make_campsite(
                campsite_id=1,
                booking_date=datetime(2026, 7, 4),
            ),
            make_campsite(
                campsite_id=1,
                booking_date=datetime(2026, 7, 4),
            ),
            make_campsite(
                campsite_id=2,
                booking_date=datetime(2026, 7, 11),
            ),
            make_campsite(
                campsite_id=3,
                booking_date=datetime(2026, 7, 11),
                campsite_type="BOAT-IN",
            ),
        ]

        processed = process_results({"alert": True}, results, None)

        assert len(processed.campsites) == 2
        assert processed.total_sites == 2
        assert processed.campsite_ids_by_date[date(2026, 7, 4)] == frozenset({1})
        assert processed.campsite_ids_by_date[date(2026, 7, 11)] == frozenset({2})
        assert len(processed.notification_keys) == 2

    def test_day_filter_is_applied_before_normalization(self):
        results = [
            make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4)),
            make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 6)),
        ]

        processed = process_results({}, results, {5})

        assert [result.campsite_id for result in processed.campsites] == [1]


# ── format_results ──────────────────────────────────────────────────────────


class TestFormatResults:
    def test_formatted_output(self):
        results = [make_campsite(booking_date=datetime(2026, 7, 4))]
        formatted = format_results({}, results, None)
        assert formatted is not None
        assert "Test Area — Test Campground" in formatted
        assert "1 open site(s)" in formatted

    def test_none_for_no_results(self):
        assert format_results({}, [], None) is None

    def test_includes_booking_url(self):
        results = [make_campsite(booking_url="https://example.com/book")]
        formatted = format_results({}, results, None)
        assert "https://example.com/book" in formatted
