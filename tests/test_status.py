"""Tests for campsite_checker.status (live scan counters)."""

from datetime import datetime, timedelta, timezone

from campsite_checker.metrics import CampgroundMetric
from campsite_checker.status import ScanStatus


class TestScanStatusHealth:
    def test_healthy_before_first_scan(self):
        status = ScanStatus()
        assert status.is_healthy() is True

    def test_healthy_after_recent_scan(self):
        status = ScanStatus()
        status.update(entries_count=5)
        assert status.is_healthy() is True

    def test_unhealthy_after_stale_scan(self):
        status = ScanStatus()
        status.alert_interval_minutes = 5
        # Simulate a scan that happened long ago
        status.last_scan_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        assert status.is_healthy() is False

    def test_alert_timestamp_drives_health_when_available(self):
        status = ScanStatus()
        status.alert_interval_minutes = 1
        status.last_scan_time = datetime.now(timezone.utc)
        status.last_alert_scan = datetime.now(timezone.utc) - timedelta(minutes=3)

        assert status.is_healthy() is False


class TestScanStatusUpdate:
    def test_increments_scan_count(self):
        status = ScanStatus()
        assert status.scan_count == 0
        status.update(entries_count=3)
        assert status.scan_count == 1
        status.update(entries_count=5)
        assert status.scan_count == 2

    def test_sets_entries_count(self):
        status = ScanStatus()
        status.update(entries_count=7)
        assert status.entries_count == 7

    def test_counts_errors(self):
        status = ScanStatus()
        status.update(entries_count=3, error=True)
        assert status.error_count == 1
        status.update(entries_count=3, error=True)
        assert status.error_count == 2
        status.update(entries_count=3, error=False)
        assert status.error_count == 2

    def test_sets_last_scan_time(self):
        status = ScanStatus()
        assert status.last_scan_time is None
        status.update(entries_count=1)
        assert status.last_scan_time is not None

    def test_records_latest_availability_and_duration(self):
        status = ScanStatus()
        status.update(
            entries_count=7,
            available_entries_count=2,
            available_sites_count=11,
            duration_seconds=3.5,
        )
        assert status.available_entries_count == 2
        assert status.available_sites_count == 11
        assert status.last_scan_duration_seconds == 3.5

    def test_error_update_preserves_last_known_gauges(self):
        """A failed cycle must not zero the monitored/availability gauges."""
        status = ScanStatus()
        status.update(
            entries_count=7,
            available_entries_count=2,
            available_sites_count=11,
            campgrounds=[
                CampgroundMetric.from_entry(
                    {"campground_id": 1},
                    config_index=0,
                    available=True,
                    available_sites=11,
                )
            ],
        )
        status.update(duration_seconds=1.0, error=True)

        assert status.error_count == 1
        assert status.entries_count == 7
        assert status.available_entries_count == 2
        assert status.available_sites_count == 11
        assert len(status.campgrounds) == 1

    def test_notification_counters_accumulate(self):
        status = ScanStatus()
        status.record_notifications(sent=2)
        status.record_notifications(sent=1, failed=3)
        assert status.notifications_sent == 3
        assert status.notifications_failed == 3
        metrics = status.to_prometheus()
        assert "campsite_checker_notifications_sent_total 3" in metrics
        assert "campsite_checker_notifications_failed_total 3" in metrics
        d = status.to_dict()
        assert d["notifications_sent"] == 3
        assert d["notifications_failed"] == 3

    def test_dashboard_publish_status_records_upload_attempts(self):
        status = ScanStatus()
        status.start_dashboard_publish()
        status.finish_dashboard_publish(
            duration_seconds=2.0,
            render_duration_seconds=0.5,
            upload_duration_seconds=1.5,
            upload_succeeded=True,
        )

        assert status.dashboard_publish_in_progress is False
        assert status.dashboard_publish_count == 1
        assert status.dashboard_publish_error_count == 0
        assert status.r2_upload_count == 1
        assert status.r2_upload_failure_count == 0

    def test_bot_liveness_reported_in_health_payload(self):
        status = ScanStatus()
        assert status.to_dict()["bot_polling_alive"] is None

        class FakeThread:
            def is_alive(self):
                return True

        status.bot_thread = FakeThread()
        assert status.to_dict()["bot_polling_alive"] is True


class TestScanStatusToDict:
    def test_initial_state(self):
        status = ScanStatus()
        d = status.to_dict()
        assert d["status"] == "ok"
        assert d["scan_count"] == 0
        assert d["error_count"] == 0
        assert d["entries_count"] == 0
        assert d["last_scan"] is None
        assert d["last_alert_scan"] is None

    def test_after_update(self):
        status = ScanStatus()
        status.update(entries_count=5)
        d = status.to_dict()
        assert d["scan_count"] == 1
        assert d["entries_count"] == 5
        assert d["last_scan"] is not None

    def test_unhealthy_status(self):
        status = ScanStatus()
        status.alert_interval_minutes = 5
        status.last_scan_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        d = status.to_dict()
        assert d["status"] == "unhealthy"
