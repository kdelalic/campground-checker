import http.server
import json
import logging
import os
import socketserver
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class _ScanStatus:
    """Tracks scan metrics for the health check endpoint."""

    def __init__(self):
        self.start_time = datetime.now(timezone.utc)
        self.last_scan_time: datetime | None = None
        self.scan_count: int = 0
        self.error_count: int = 0
        self.entries_count: int = 0
        self.alert_interval_minutes: int = 5
        self.dashboard_alert_interval_minutes: int = 60
        self.last_dashboard_scan: datetime | None = None

    def update(self, *, entries_count: int, error: bool = False) -> None:
        self.last_scan_time = datetime.now(timezone.utc)
        self.scan_count += 1
        self.entries_count = entries_count
        if error:
            self.error_count += 1

    def is_healthy(self) -> bool:
        """Healthy if we've had a scan within 2x the interval (or haven't started yet)."""
        if self.last_scan_time is None:
            return True  # Still warming up
        elapsed = (datetime.now(timezone.utc) - self.last_scan_time).total_seconds()
        return elapsed < self.alert_interval_minutes * 2 * 60

    def to_dict(self) -> dict:
        uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        return {
            "status": "ok" if self.is_healthy() else "unhealthy",
            "uptime_seconds": int(uptime),
            "scan_count": self.scan_count,
            "error_count": self.error_count,
            "entries_count": self.entries_count,
            "last_scan": self.last_scan_time.isoformat() if self.last_scan_time else None,
            "dashboard_alert_interval_minutes": self.dashboard_alert_interval_minutes,
            "last_dashboard_scan": (
                self.last_dashboard_scan.isoformat() if self.last_dashboard_scan else None
            ),
        }


scan_status = _ScanStatus()


class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        data = scan_status.to_dict()
        code = 200 if scan_status.is_healthy() else 503
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default per-request logging."""
        pass


def start_healthcheck_server() -> None:
    """Start a simple HTTP server to pass health checks (e.g. Railway, Koyeb)."""
    port = int(os.environ.get("PORT", "8000"))
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("", port), HealthCheckHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        logger.info("Healthcheck HTTP server running on port %d", port)
    except Exception as e:
        logger.error("Failed to start healthcheck server on port %d: %s", port, e)
