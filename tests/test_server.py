"""Tests for campsite_checker.server."""

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from campsite_checker.server import CampgroundMetric, HealthCheckHandler, _ScanStatus
from campsite_checker.throttle import ProviderThrottleRegistry


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

    def test_alert_timestamp_drives_health_when_available(self):
        status = _ScanStatus()
        status.alert_interval_minutes = 1
        status.last_scan_time = datetime.now(timezone.utc)
        status.last_alert_scan = datetime.now(timezone.utc) - timedelta(minutes=3)

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

    def test_records_latest_availability_and_duration(self):
        status = _ScanStatus()
        status.update(
            entries_count=7,
            available_entries_count=2,
            available_sites_count=11,
            duration_seconds=3.5,
        )
        assert status.available_entries_count == 2
        assert status.available_sites_count == 11
        assert status.last_scan_duration_seconds == 3.5


class TestScanStatusToDict:
    def test_initial_state(self):
        status = _ScanStatus()
        d = status.to_dict()
        assert d["status"] == "ok"
        assert d["scan_count"] == 0
        assert d["error_count"] == 0
        assert d["entries_count"] == 0
        assert d["last_scan"] is None
        assert d["last_alert_scan"] is None

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


class TestPrometheusMetrics:
    def test_initial_metrics(self):
        metrics = _ScanStatus().to_prometheus()
        assert "# TYPE campsite_checker_scans_total counter" in metrics
        assert "campsite_checker_scans_total 0" in metrics
        assert "campsite_checker_up 1" in metrics
        assert "campsite_checker_last_scan_timestamp_seconds 0" in metrics
        assert "campsite_checker_last_alert_scan_timestamp_seconds 0" in metrics

    def test_alert_scan_timestamp_metric(self):
        status = _ScanStatus()
        completed_at = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        status.mark_alert_scan(completed_at)

        metrics = status.to_prometheus()

        assert (
            f"campsite_checker_last_alert_scan_timestamp_seconds {completed_at.timestamp()}"
            in metrics
        )

    def test_dashboard_worker_lifecycle_metrics(self):
        status = _ScanStatus()
        status.start_dashboard_scan()

        running_metrics = status.to_prometheus()
        assert "campsite_checker_dashboard_scan_in_progress 1" in running_metrics

        completed_at = datetime(2026, 7, 26, 12, 5, tzinfo=timezone.utc)
        status.finish_dashboard_scan(
            duration_seconds=125.5,
            error=True,
            when=completed_at,
        )
        metrics = status.to_prometheus()

        assert "campsite_checker_dashboard_scan_in_progress 0" in metrics
        assert "campsite_checker_dashboard_scans_total 1" in metrics
        assert "campsite_checker_dashboard_scan_errors_total 1" in metrics
        assert "campsite_checker_last_dashboard_scan_duration_seconds 125.5" in metrics
        assert (
            f"campsite_checker_last_dashboard_scan_timestamp_seconds {completed_at.timestamp()}"
        ) in metrics

    def test_metrics_after_scan(self):
        status = _ScanStatus()
        status.update(
            entries_count=5,
            available_entries_count=2,
            available_sites_count=9,
            duration_seconds=1.25,
            error=True,
        )
        metrics = status.to_prometheus()
        assert "campsite_checker_scans_total 1" in metrics
        assert "campsite_checker_scan_errors_total 1" in metrics
        assert "campsite_checker_campgrounds_monitored 5" in metrics
        assert "campsite_checker_campgrounds_available 2" in metrics
        assert "campsite_checker_campsites_available 9" in metrics
        assert "campsite_checker_last_scan_duration_seconds 1.25" in metrics
        assert not metrics.endswith("\n\n")

    def test_per_campground_metrics(self):
        status = _ScanStatus()
        campground = CampgroundMetric.from_entry(
            {
                "provider": "RecreationDotGov",
                "campground_id": 232447,
                "campsite_id": [42, 43],
                "name": 'Upper "Pines"\\Camp',
                "alert": True,
            },
            config_index=3,
            available=True,
            available_sites=7,
            scan_success=False,
        )
        status.update(
            entries_count=1,
            available_entries_count=1,
            available_sites_count=7,
            campgrounds=[campground],
        )

        metrics = status.to_prometheus()
        labels = (
            '{config_index="3",provider="RecreationDotGov",campground_id="232447",'
            'recreation_area="",campsite_id="42,43",name="Upper \\"Pines\\"\\\\Camp",'
            'alert="true"}'
        )
        assert f"campsite_checker_campground_available{labels} 1" in metrics
        assert f"campsite_checker_campground_campsites_available{labels} 7" in metrics
        assert f"campsite_checker_campground_last_scan_success{labels} 0" in metrics

    def test_campground_name_falls_back_to_stable_id(self):
        campground = CampgroundMetric.from_entry(
            {"provider": "ReserveCalifornia", "campground_id": 786},
            config_index=0,
            available=False,
            available_sites=0,
        )
        assert campground.name == "786"

    def test_metrics_endpoint(self, monkeypatch):
        status = _ScanStatus()
        status.update(entries_count=3)
        monkeypatch.setattr("campsite_checker.server.scan_status", status)

        handler = HealthCheckHandler.__new__(HealthCheckHandler)
        handler.path = "/metrics?source=test"
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()

        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call(
            "Content-Type",
            "text/plain; version=0.0.4; charset=utf-8",
        )
        assert b"campsite_checker_scans_total 1" in handler.wfile.getvalue()

    def test_provider_throttle_metrics(self):
        registry = ProviderThrottleRegistry(clock=lambda: 100)
        registry.ensure("ReserveCalifornia")
        registry.record_rate_limit("RecreationDotGov", retry_after_seconds=75)
        status = _ScanStatus(throttle_registry=registry)

        metrics = status.to_prometheus()

        assert (
            'campsite_checker_provider_rate_limit_events_total{provider="RecreationDotGov"} 1'
        ) in metrics
        assert (
            'campsite_checker_provider_throttle_cooldown_seconds{provider="RecreationDotGov"} 75'
        ) in metrics
        assert (
            "campsite_checker_provider_throttle_last_backoff_seconds"
            '{provider="RecreationDotGov"} 75'
        ) in metrics
        assert (
            'campsite_checker_provider_consecutive_rate_limits{provider="RecreationDotGov"} 1'
        ) in metrics
        assert (
            'campsite_checker_provider_rate_limit_events_total{provider="ReserveCalifornia"} 0'
        ) in metrics
