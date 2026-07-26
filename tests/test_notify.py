"""Tests for campsite_checker.notify."""

from datetime import datetime

from campsite_checker.notify import (
    _MAX_TG_LEN,
    build_processed_telegram_message,
    build_telegram_message,
    filter_new_results,
    result_keys,
)
from campsite_checker.results import process_results

from .conftest import make_campsite

# ── build_telegram_message ──────────────────────────────────────────────────


class TestBuildTelegramMessage:
    def test_empty_input_returns_empty(self):
        assert build_telegram_message([], None) == []

    def test_no_results_returns_empty(self):
        # All entries have results filtered out by day filter
        results = [make_campsite(campsite_type="BOAT-IN")]
        entries_with_results = [({"name": "Test"}, results)]
        assert build_telegram_message(entries_with_results, None) == []

    def test_single_campground(self):
        results = [make_campsite(booking_date=datetime(2026, 7, 4))]
        entries_with_results = [({"name": "Test Camp"}, results)]
        msgs = build_telegram_message(entries_with_results, None)
        assert len(msgs) == 1
        assert "<b>Campsite Availability Found!</b>" in msgs[0]
        assert "Test Area — Test Campground" in msgs[0]
        assert "1 open site(s)" in msgs[0]

    def test_multiple_campgrounds_in_one_message(self):
        results1 = [make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4))]
        results2 = [make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 11))]
        entries = [({"name": "Camp A"}, results1), ({"name": "Camp B"}, results2)]
        msgs = build_telegram_message(entries, None)
        assert len(msgs) == 1
        assert "Test Area — Test Campground" in msgs[0]

    def test_message_splitting_at_4096(self):
        """When content exceeds 4096 chars, it should split into multiple messages."""
        # Create many campgrounds with many results to exceed 4096 chars
        entries = []
        for i in range(50):
            results = [
                make_campsite(
                    campsite_id=i * 100 + j,
                    facility_name=f"Campground Number {i}",
                    booking_date=datetime(2026, 7, j + 1),
                )
                for j in range(5)
            ]
            entries.append(({"name": f"Camp {i}"}, results))
        msgs = build_telegram_message(entries, None)
        assert len(msgs) >= 2
        for msg in msgs:
            assert len(msg) <= _MAX_TG_LEN

    def test_html_escaping(self):
        results = [
            make_campsite(
                facility_name="<script>alert('xss')</script>",
                recreation_area="",
                booking_url="javascript:void(0)",
            )
        ]
        entries = [({"name": "Test"}, results)]
        msgs = build_telegram_message(entries, None)
        assert "<script>" not in msgs[0]
        assert "&lt;script&gt;" in msgs[0]

    def test_formats_preprocessed_availability(self):
        processed = process_results(
            {"name": "Test"},
            [make_campsite(booking_date=datetime(2026, 7, 4))],
            None,
        )

        msgs = build_processed_telegram_message([processed])

        assert len(msgs) == 1
        assert "Test Area — Test Campground" in msgs[0]
        assert "1 open site(s)" in msgs[0]


# ── result_keys ─────────────────────────────────────────────────────────────


class TestResultKeys:
    def test_keys_generated(self):
        results = [
            make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4)),
            make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 4)),
        ]
        keys = result_keys({}, results, None)
        assert len(keys) == 2
        for k in keys:
            assert isinstance(k, tuple)
            assert len(k) == 3  # (name, campsite_id, date)

    def test_empty_results(self):
        keys = result_keys({}, [], None)
        assert len(keys) == 0

    def test_filtered_results_excluded(self):
        results = [make_campsite(campsite_type="BOAT-IN")]
        keys = result_keys({}, results, None)
        assert len(keys) == 0


# ── filter_new_results ──────────────────────────────────────────────────────


class TestFilterNewResults:
    def test_alert_disabled_returns_empty(self):
        entry = {"alert": False}
        results = [make_campsite()]
        assert filter_new_results(entry, results, None, set()) == []

    def test_all_new_results(self):
        entry = {"alert": True}
        results = [make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4))]
        new = filter_new_results(entry, results, None, set())
        assert len(new) == 1

    def test_previously_sent_filtered(self):
        entry = {"alert": True}
        results = [make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4))]
        # Build a prev_keys set matching the result
        from campsite_checker.results import get_facility_name

        filtered = [r for r in results if r.campsite_type != "BOAT-IN"]
        name = get_facility_name(filtered)
        prev = {(name, 1, datetime(2026, 7, 4).date())}
        new = filter_new_results(entry, results, None, prev)
        assert len(new) == 0

    def test_mixed_new_and_old(self):
        entry = {"alert": True}
        results = [
            make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4)),
            make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 4)),
        ]
        from campsite_checker.results import get_facility_name

        name = get_facility_name(results)
        prev = {(name, 1, datetime(2026, 7, 4).date())}
        new = filter_new_results(entry, results, None, prev)
        assert len(new) == 1
        assert new[0].campsite_id == 2
