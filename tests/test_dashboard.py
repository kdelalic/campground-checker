"""Tests for dashboard rendering from normalized availability."""

from datetime import datetime, timezone

from campsite_checker.dashboard import build_dashboard_html
from campsite_checker.results import process_results

from .conftest import make_campsite


def test_dashboard_reuses_preprocessed_availability(monkeypatch):
    processed = process_results(
        {"name": "Configured Name", "campground_id": 100},
        [make_campsite(booking_url="https://example.com/book")],
        None,
    )

    def fail_if_reprocessed(*args, **kwargs):
        raise AssertionError("processed availability should not be filtered again")

    monkeypatch.setattr("campsite_checker.dashboard.process_results", fail_if_reprocessed)

    content = build_dashboard_html(
        [processed],
        None,
        scan_timestamp=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )

    assert "Test Area — Test Campground" in content
    assert "1 open site(s)" in content
    assert "https://example.com/book" in content
