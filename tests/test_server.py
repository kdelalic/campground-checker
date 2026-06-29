"""Tests for campsite_checker.server."""

from datetime import datetime, timedelta, timezone

from campsite_checker.server import _ScanStatus


class TestScanStatusHealth:
    def test_healthy_before_first_scan(self):
        status = _ScanStatus()
        assert status.is_healthy() is True

    def test_healthy_after_recent_scan(self):
        status = _ScanStatus()
        status.update(entries_count=5)
        assert status.is_healthy() is True

    def test_unhealthy_after_stale_scan(self):
        status = _ScanStatus()
        status.alert_interval_minutes = 5
        # Simulate a scan that happened long ago
        status.last_scan_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        assert status.is_healthy() is False


class TestScanStatusUpdate:
    def test_increments_scan_count(self):
        status = _ScanStatus()
        assert status.scan_count == 0
        status.update(entries_count=3)
        assert status.scan_count == 1
        status.update(entries_count=5)
        assert status.scan_count == 2

    def test_sets_entries_count(self):
        status = _ScanStatus()
        status.update(entries_count=7)
        assert status.entries_count == 7

    def test_counts_errors(self):
        status = _ScanStatus()
        status.update(entries_count=3, error=True)
        assert status.error_count == 1
        status.update(entries_count=3, error=True)
        assert status.error_count == 2
        status.update(entries_count=3, error=False)
        assert status.error_count == 2

    def test_sets_last_scan_time(self):
        status = _ScanStatus()
        assert status.last_scan_time is None
        status.update(entries_count=1)
        assert status.last_scan_time is not None


class TestScanStatusToDict:
    def test_initial_state(self):
        status = _ScanStatus()
        d = status.to_dict()
        assert d["status"] == "ok"
        assert d["scan_count"] == 0
        assert d["error_count"] == 0
        assert d["entries_count"] == 0
        assert d["last_scan"] is None

    def test_after_update(self):
        status = _ScanStatus()
        status.update(entries_count=5)
        d = status.to_dict()
        assert d["scan_count"] == 1
        assert d["entries_count"] == 5
        assert d["last_scan"] is not None

    def test_unhealthy_status(self):
        status = _ScanStatus()
        status.alert_interval_minutes = 5
        status.last_scan_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        d = status.to_dict()
        assert d["status"] == "unhealthy"
