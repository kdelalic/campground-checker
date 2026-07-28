"""Tests for campsite_checker.metrics (Prometheus exposition format)."""

from datetime import datetime, timezone

from campsite_checker.metrics import (
    CAMPGROUND_SCAN_FAILURES,
    CampgroundMetric,
    CampgroundScanFailures,
)
from campsite_checker.providers.recreation_gov import ProviderRequestMetrics
from campsite_checker.status import ScanStatus
from campsite_checker.throttle import ProviderThrottleRegistry


class TestPrometheusMetrics:
    def test_initial_metrics(self):
        metrics = ScanStatus().to_prometheus()
        assert "# TYPE campsite_checker_scans_total counter" in metrics
        assert "campsite_checker_scans_total 0" in metrics
        assert "campsite_checker_up 1" in metrics
        assert "campsite_checker_last_scan_timestamp_seconds 0" in metrics
        assert "campsite_checker_last_alert_scan_timestamp_seconds 0" in metrics

    def test_alert_scan_timestamp_metric(self):
        status = ScanStatus()
        completed_at = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        status.mark_alert_scan(completed_at)

        metrics = status.to_prometheus()

        assert (
            f"campsite_checker_last_alert_scan_timestamp_seconds {completed_at.timestamp()}"
            in metrics
        )

    def test_dashboard_worker_lifecycle_metrics(self):
        status = ScanStatus()
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
        status = ScanStatus()
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
        status = ScanStatus()
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

    def test_campground_scan_failures_are_exported_as_a_counter(self):
        failures = CampgroundScanFailures()
        failures.record_failure(3)
        failures.record_failure(3)
        failures.record_failure(4)

        assert failures.get(3) == 2
        assert failures.get(4) == 1
        assert failures.get(99) == 0

        status = ScanStatus()
        status.update(
            campgrounds=[
                CampgroundMetric.from_entry(
                    {"provider": "RecreationDotGov", "campground_id": 232447},
                    config_index=3,
                    available=False,
                    available_sites=0,
                    scan_success=False,
                    scan_failures=failures.get(3),
                )
            ],
        )

        metrics = status.to_prometheus()
        assert "# TYPE campsite_checker_campground_scan_failures_total counter" in metrics
        assert 'campsite_checker_campground_scan_failures_total{config_index="3"' in metrics
        assert metrics.count("campsite_checker_campground_scan_failures_total") == 3

    def test_campground_scan_failures_default_to_the_process_registry(self):
        CAMPGROUND_SCAN_FAILURES.clear()
        CAMPGROUND_SCAN_FAILURES.record_failure(7)
        try:
            campground = CampgroundMetric.from_entry(
                {"provider": "RecreationDotGov", "campground_id": 232447},
                config_index=7,
                available=False,
                available_sites=0,
            )
            assert campground.scan_failures == 1
        finally:
            CAMPGROUND_SCAN_FAILURES.clear()

    def test_campground_name_falls_back_to_stable_id(self):
        campground = CampgroundMetric.from_entry(
            {"provider": "ReserveCalifornia", "campground_id": 786},
            config_index=0,
            available=False,
            available_sites=0,
        )
        assert campground.name == "786"

    def test_provider_throttle_metrics(self):
        registry = ProviderThrottleRegistry(clock=lambda: 100)
        registry.ensure("ReserveCalifornia")
        registry.record_rate_limit("RecreationDotGov", retry_after_seconds=75)
        status = ScanStatus(throttle_registry=registry)

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

    def test_native_provider_request_metrics(self):
        request_metrics = ProviderRequestMetrics()
        request_metrics.record_attempt("RecreationDotGov")
        request_metrics.record_attempt("RecreationDotGov")
        request_metrics.record_retry("RecreationDotGov")
        request_metrics.record_failure("RecreationDotGov")
        status = ScanStatus(request_metrics=request_metrics)

        metrics = status.to_prometheus()

        assert (
            'campsite_checker_provider_request_attempts_total{provider="RecreationDotGov"} 2'
        ) in metrics
        assert (
            'campsite_checker_provider_request_retries_total{provider="RecreationDotGov"} 1'
        ) in metrics
        assert (
            'campsite_checker_provider_request_failures_total{provider="RecreationDotGov"} 1'
        ) in metrics
        assert status.to_dict()["provider_requests"][0]["retries"] == 1
