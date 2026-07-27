"""Tests for dashboard rendering from normalized availability."""

from datetime import datetime, timezone
from types import SimpleNamespace

from campsite_checker.dashboard import DashboardPublisher, build_dashboard_html
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


class FakeUploader:
    def __init__(self, successes):
        self.successes = iter(successes)
        self.calls = 0

    def upload(self, output_path):
        self.calls += 1
        return SimpleNamespace(
            success=next(self.successes),
            public_url="https://example.com/dashboard",
        )


def test_publisher_skips_unchanged_write_and_upload(tmp_path):
    output_path = tmp_path / "dashboard.html"
    uploader = FakeUploader([True])
    publisher = DashboardPublisher(str(output_path), uploader)
    processed = process_results({}, [make_campsite(campsite_id=1)], None)

    first = publisher.publish([processed])
    second = publisher.publish([processed])

    assert first.written is True
    assert first.uploaded is True
    assert second.written is False
    assert second.uploaded is False
    assert uploader.calls == 1


def test_publisher_retries_failed_upload_without_rewriting(tmp_path):
    output_path = tmp_path / "dashboard.html"
    uploader = FakeUploader([False, True])
    publisher = DashboardPublisher(str(output_path), uploader)
    processed = process_results({}, [make_campsite(campsite_id=1)], None)

    first = publisher.publish([processed])
    second = publisher.publish([processed])

    assert first.written is True
    assert first.uploaded is False
    assert second.written is False
    assert second.uploaded is True
    assert uploader.calls == 2


def test_publisher_rewrites_when_availability_changes(tmp_path):
    output_path = tmp_path / "dashboard.html"
    publisher = DashboardPublisher(str(output_path))
    first = process_results({}, [make_campsite(campsite_id=1)], None)
    changed = process_results({}, [make_campsite(campsite_id=2)], None)

    assert publisher.publish([first]).written is True
    assert publisher.publish([changed]).written is True


def test_publisher_republishes_unchanged_content_once_stale(tmp_path):
    """The page's "Last updated" is its only liveness signal, so unchanged
    availability is still republished after the freshness interval."""
    output_path = tmp_path / "dashboard.html"
    clock_now = [0.0]
    uploader = FakeUploader([True, True])
    publisher = DashboardPublisher(
        str(output_path),
        uploader,
        freshness_interval_seconds=3600,
        clock=lambda: clock_now[0],
    )
    processed = process_results({}, [make_campsite(campsite_id=1)], None)

    assert publisher.publish([processed]).written is True
    clock_now[0] = 1800.0
    mid = publisher.publish([processed])
    assert mid.written is False
    assert mid.uploaded is False
    clock_now[0] = 3700.0
    stale = publisher.publish([processed])
    assert stale.written is True
    assert stale.uploaded is True
    assert uploader.calls == 2
