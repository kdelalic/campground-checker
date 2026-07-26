import http.server
import json
import logging
import os
import socketserver
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .throttle import PROVIDER_THROTTLES, ProviderThrottleRegistry

logger = logging.getLogger(__name__)


def _stringify_label(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@dataclass(frozen=True, slots=True)
class CampgroundMetric:
    config_index: int
    provider: str
    campground_id: str
    recreation_area: str
    campsite_id: str
    name: str
    alert: bool
    available: bool
    available_sites: int
    scan_success: bool

    @classmethod
    def from_entry(
        cls,
        entry: dict,
        *,
        config_index: int,
        available: bool,
        available_sites: int,
        scan_success: bool = True,
    ) -> "CampgroundMetric":
        provider = _stringify_label(entry.get("provider", "RecreationDotGov"))
        campground_id = _stringify_label(entry.get("campground_id"))
        recreation_area = _stringify_label(entry.get("recreation_area"))
        campsite_id = _stringify_label(entry.get("campsite_id"))
        name = _stringify_label(entry.get("name"))
        if not name:
            name = campground_id or recreation_area or campsite_id or f"entry-{config_index}"
        return cls(
            config_index=config_index,
            provider=provider,
            campground_id=campground_id,
            recreation_area=recreation_area,
            campsite_id=campsite_id,
            name=name,
            alert=entry.get("alert", False),
            available=available,
            available_sites=available_sites,
            scan_success=scan_success,
        )

    def labels(self) -> str:
        labels = (
            ("config_index", str(self.config_index)),
            ("provider", self.provider),
            ("campground_id", self.campground_id),
            ("recreation_area", self.recreation_area),
            ("campsite_id", self.campsite_id),
            ("name", self.name),
            ("alert", str(self.alert).lower()),
        )
        rendered = ",".join(f'{key}="{_escape_label_value(value)}"' for key, value in labels)
        return f"{{{rendered}}}"


class _ScanStatus:
    """Tracks scan metrics for the health check endpoint."""

    def __init__(self, throttle_registry: ProviderThrottleRegistry | None = None):
        self._lock = threading.RLock()
        self.throttle_registry = throttle_registry or PROVIDER_THROTTLES
        self.start_time = datetime.now(timezone.utc)
        self.last_scan_time: datetime | None = None
        self.scan_count: int = 0
        self.error_count: int = 0
        self.entries_count: int = 0
        self.available_entries_count: int = 0
        self.available_sites_count: int = 0
        self.campgrounds: tuple[CampgroundMetric, ...] = ()
        self.last_scan_duration_seconds: float = 0
        self.alert_interval_minutes: int = 5
        self.dashboard_interval_minutes: int = 60
        self.last_dashboard_scan: datetime | None = None

    @property
    def dashboard_alert_interval_minutes(self) -> int:
        """Backward-compatible name used by older health endpoint consumers."""
        return self.dashboard_interval_minutes

    @dashboard_alert_interval_minutes.setter
    def dashboard_alert_interval_minutes(self, value: int) -> None:
        self.dashboard_interval_minutes = value

    def update(
        self,
        *,
        entries_count: int,
        available_entries_count: int = 0,
        available_sites_count: int = 0,
        campgrounds: list[CampgroundMetric] | tuple[CampgroundMetric, ...] = (),
        duration_seconds: float = 0,
        error: bool = False,
    ) -> None:
        with self._lock:
            self.last_scan_time = datetime.now(timezone.utc)
            self.scan_count += 1
            self.entries_count = entries_count
            self.available_entries_count = available_entries_count
            self.available_sites_count = available_sites_count
            self.campgrounds = tuple(campgrounds)
            self.last_scan_duration_seconds = duration_seconds
            if error:
                self.error_count += 1

    def mark_dashboard_scan(self) -> None:
        with self._lock:
            self.last_dashboard_scan = datetime.now(timezone.utc)

    def is_healthy(self) -> bool:
        """Healthy if we've had a scan within 2x the interval (or haven't started yet)."""
        with self._lock:
            if self.last_scan_time is None:
                return True  # Still warming up
            elapsed = (datetime.now(timezone.utc) - self.last_scan_time).total_seconds()
            return elapsed < self.alert_interval_minutes * 2 * 60

    def to_dict(self) -> dict:
        with self._lock:
            uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()
            provider_throttles = self.throttle_registry.snapshot()
            return {
                "status": "ok" if self.is_healthy() else "unhealthy",
                "uptime_seconds": int(uptime),
                "scan_count": self.scan_count,
                "error_count": self.error_count,
                "entries_count": self.entries_count,
                "available_entries_count": self.available_entries_count,
                "available_sites_count": self.available_sites_count,
                "last_scan_duration_seconds": self.last_scan_duration_seconds,
                "last_scan": self.last_scan_time.isoformat() if self.last_scan_time else None,
                "dashboard_interval_minutes": self.dashboard_interval_minutes,
                # Retained for compatibility with the original health response.
                "dashboard_alert_interval_minutes": self.dashboard_interval_minutes,
                "last_dashboard_scan": (
                    self.last_dashboard_scan.isoformat() if self.last_dashboard_scan else None
                ),
                "provider_throttles": [
                    {
                        "provider": throttle.provider,
                        "rate_limit_events": throttle.rate_limit_events,
                        "consecutive_rate_limits": throttle.consecutive_rate_limits,
                        "cooldown_seconds": throttle.cooldown_seconds,
                        "last_backoff_seconds": throttle.last_backoff_seconds,
                    }
                    for throttle in provider_throttles
                ],
            }

    def to_prometheus(self) -> str:
        """Render the current status using Prometheus' text exposition format."""
        with self._lock:
            now = datetime.now(timezone.utc)
            uptime = (now - self.start_time).total_seconds()
            healthy = self.is_healthy()
            last_scan_timestamp = self.last_scan_time.timestamp() if self.last_scan_time else 0
            last_dashboard_timestamp = (
                self.last_dashboard_scan.timestamp() if self.last_dashboard_scan else 0
            )
            metrics = (
                (
                    "campsite_checker_up",
                    "Whether the campsite checker is healthy (1) or stale (0).",
                    "gauge",
                    int(healthy),
                ),
                (
                    "campsite_checker_uptime_seconds",
                    "Seconds since the campsite checker process started.",
                    "gauge",
                    uptime,
                ),
                (
                    "campsite_checker_scans_total",
                    "Total number of completed scan cycles.",
                    "counter",
                    self.scan_count,
                ),
                (
                    "campsite_checker_scan_errors_total",
                    "Total number of scan cycles that ended with an error.",
                    "counter",
                    self.error_count,
                ),
                (
                    "campsite_checker_campgrounds_monitored",
                    "Number of campgrounds in the most recent scan cycle.",
                    "gauge",
                    self.entries_count,
                ),
                (
                    "campsite_checker_campgrounds_available",
                    "Number of campgrounds with availability in the latest results.",
                    "gauge",
                    self.available_entries_count,
                ),
                (
                    "campsite_checker_campsites_available",
                    "Number of available campsite-date combinations in the latest results.",
                    "gauge",
                    self.available_sites_count,
                ),
                (
                    "campsite_checker_last_scan_duration_seconds",
                    "Duration of the most recent scan cycle in seconds.",
                    "gauge",
                    self.last_scan_duration_seconds,
                ),
                (
                    "campsite_checker_last_scan_timestamp_seconds",
                    "Unix timestamp of the most recent completed scan cycle.",
                    "gauge",
                    last_scan_timestamp,
                ),
                (
                    "campsite_checker_alert_interval_seconds",
                    "Configured interval between alert scan cycles in seconds.",
                    "gauge",
                    self.alert_interval_minutes * 60,
                ),
                (
                    "campsite_checker_dashboard_interval_seconds",
                    "Configured interval between dashboard scans in seconds.",
                    "gauge",
                    self.dashboard_interval_minutes * 60,
                ),
                (
                    "campsite_checker_last_dashboard_scan_timestamp_seconds",
                    "Unix timestamp of the most recent dashboard scan.",
                    "gauge",
                    last_dashboard_timestamp,
                ),
            )
            campgrounds = self.campgrounds
            provider_throttles = self.throttle_registry.snapshot()

        lines = []
        for name, help_text, metric_type, value in metrics:
            lines.extend(
                (
                    f"# HELP {name} {help_text}",
                    f"# TYPE {name} {metric_type}",
                    f"{name} {value}",
                )
            )
        campground_metrics = (
            (
                "campsite_checker_campground_available",
                "Whether the configured campground has availability in its latest results.",
                lambda campground: int(campground.available),
            ),
            (
                "campsite_checker_campground_campsites_available",
                "Available campsite-date combinations for the configured campground.",
                lambda campground: campground.available_sites,
            ),
            (
                "campsite_checker_campground_last_scan_success",
                "Whether the latest search for the configured campground succeeded.",
                lambda campground: int(campground.scan_success),
            ),
        )
        for name, help_text, get_value in campground_metrics:
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
            lines.extend(
                f"{name}{campground.labels()} {get_value(campground)}" for campground in campgrounds
            )
        provider_metrics = (
            (
                "campsite_checker_provider_rate_limit_events_total",
                "Provider rate-limit responses observed after request retries were exhausted.",
                "counter",
                lambda throttle: throttle.rate_limit_events,
            ),
            (
                "campsite_checker_provider_throttle_cooldown_seconds",
                "Seconds remaining in the adaptive provider cooldown.",
                "gauge",
                lambda throttle: throttle.cooldown_seconds,
            ),
            (
                "campsite_checker_provider_throttle_last_backoff_seconds",
                "Most recent adaptive backoff applied for the provider in seconds.",
                "gauge",
                lambda throttle: throttle.last_backoff_seconds,
            ),
            (
                "campsite_checker_provider_consecutive_rate_limits",
                "Consecutive provider rate limits without a subsequent successful request.",
                "gauge",
                lambda throttle: throttle.consecutive_rate_limits,
            ),
        )
        for name, help_text, metric_type, get_value in provider_metrics:
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))
            lines.extend(
                f'{name}{{provider="{_escape_label_value(throttle.provider)}"}} '
                f"{get_value(throttle)}"
                for throttle in provider_throttles
            )
        return "\n".join(lines) + "\n"


scan_status = _ScanStatus()


class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.partition("?")[0] == "/metrics":
            body = scan_status.to_prometheus().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        data = scan_status.to_dict()
        code = 200 if scan_status.is_healthy() else 503
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
