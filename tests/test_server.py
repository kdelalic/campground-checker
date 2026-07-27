"""Tests for campsite_checker.server (health and metrics HTTP endpoints)."""

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from campsite_checker.server import HealthCheckHandler
from campsite_checker.status import ScanStatus


class TestHealthCheckHandler:
    def test_metrics_endpoint(self, monkeypatch):
        status = ScanStatus()
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

    def test_health_endpoint_reports_unhealthy(self, monkeypatch):
        status = ScanStatus()
        status.alert_interval_minutes = 5
        status.last_scan_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        monkeypatch.setattr("campsite_checker.server.scan_status", status)

        handler = HealthCheckHandler.__new__(HealthCheckHandler)
        handler.path = "/"
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()

        handler.send_response.assert_called_once_with(503)
        assert b'"status": "unhealthy"' in handler.wfile.getvalue()
