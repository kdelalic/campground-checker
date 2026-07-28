"""Live scan counters behind the health and metrics endpoints.

`ScanStatus` is the single mutable observability object: the runner writes to
it, `server.py` reads it. Rendering lives in `metrics.py`.
"""

import threading
from datetime import datetime, timezone

from .metrics import CampgroundMetric, MetricsSnapshot, _timestamp, render_prometheus
from .request_gate import PROVIDER_REQUEST_METRICS, ProviderRequestMetrics
from .throttle import PROVIDER_THROTTLES, ProviderThrottleRegistry


class ScanStatus:
    """Tracks scan metrics for the health check endpoint."""

    def __init__(
        self,
        throttle_registry: ProviderThrottleRegistry | None = None,
        request_metrics: ProviderRequestMetrics | None = None,
    ):
        self._lock = threading.RLock()
        self.throttle_registry = throttle_registry or PROVIDER_THROTTLES
        self.request_metrics = request_metrics or PROVIDER_REQUEST_METRICS
        self.start_time = datetime.now(timezone.utc)
        self.last_scan_time: datetime | None = None
        self.last_alert_scan: datetime | None = None
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
        self.dashboard_scan_count: int = 0
        self.dashboard_scan_error_count: int = 0
        self.dashboard_scan_in_progress: bool = False
        self.last_dashboard_scan_duration_seconds: float = 0
        self.notifications_sent: int = 0
        self.notifications_failed: int = 0
        # Set by the runner when Telegram bot polling starts; None = no bot.
        self.bot_thread: threading.Thread | None = None

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
        entries_count: int | None = None,
        available_entries_count: int | None = None,
        available_sites_count: int | None = None,
        campgrounds: list[CampgroundMetric] | tuple[CampgroundMetric, ...] | None = None,
        duration_seconds: float = 0,
        error: bool = False,
    ) -> None:
        """Record a completed scan cycle.

        Fields left as None keep their previous values, so an error-only
        update does not zero the last known availability gauges.
        """
        with self._lock:
            self.last_scan_time = datetime.now(timezone.utc)
            self.scan_count += 1
            if entries_count is not None:
                self.entries_count = entries_count
            if available_entries_count is not None:
                self.available_entries_count = available_entries_count
            if available_sites_count is not None:
                self.available_sites_count = available_sites_count
            if campgrounds is not None:
                self.campgrounds = tuple(campgrounds)
            self.last_scan_duration_seconds = duration_seconds
            if error:
                self.error_count += 1

    def record_notifications(self, *, sent: int = 0, failed: int = 0) -> None:
        with self._lock:
            self.notifications_sent += sent
            self.notifications_failed += failed

    def mark_alert_scan(self, when: datetime | None = None) -> None:
        with self._lock:
            self.last_alert_scan = when or datetime.now(timezone.utc)

    def start_dashboard_scan(self) -> None:
        with self._lock:
            self.dashboard_scan_in_progress = True

    def finish_dashboard_scan(
        self,
        *,
        duration_seconds: float,
        error: bool = False,
        when: datetime | None = None,
    ) -> None:
        with self._lock:
            self.dashboard_scan_in_progress = False
            self.last_dashboard_scan = when or datetime.now(timezone.utc)
            self.last_dashboard_scan_duration_seconds = duration_seconds
            self.dashboard_scan_count += 1
            if error:
                self.dashboard_scan_error_count += 1

    def is_healthy(self) -> bool:
        """Healthy if we've had an alert scan within 2x its configured interval."""
        with self._lock:
            latest_alert_scan = self.last_alert_scan or self.last_scan_time
            if latest_alert_scan is None:
                return True  # Still warming up
            elapsed = (datetime.now(timezone.utc) - latest_alert_scan).total_seconds()
            return elapsed < self.alert_interval_minutes * 2 * 60

    def to_dict(self) -> dict:
        with self._lock:
            uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()
            provider_throttles = self.throttle_registry.snapshot()
            provider_requests = self.request_metrics.snapshot()
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
                "last_alert_scan": (
                    self.last_alert_scan.isoformat() if self.last_alert_scan else None
                ),
                "dashboard_interval_minutes": self.dashboard_interval_minutes,
                # Retained for compatibility with the original health response.
                "dashboard_alert_interval_minutes": self.dashboard_interval_minutes,
                "last_dashboard_scan": (
                    self.last_dashboard_scan.isoformat() if self.last_dashboard_scan else None
                ),
                "dashboard_scan_count": self.dashboard_scan_count,
                "dashboard_scan_error_count": self.dashboard_scan_error_count,
                "dashboard_scan_in_progress": self.dashboard_scan_in_progress,
                "notifications_sent": self.notifications_sent,
                "notifications_failed": self.notifications_failed,
                "bot_polling_alive": (
                    self.bot_thread.is_alive() if self.bot_thread is not None else None
                ),
                "last_dashboard_scan_duration_seconds": (self.last_dashboard_scan_duration_seconds),
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
                "provider_requests": [
                    {
                        "provider": request.provider,
                        "attempts": request.attempts,
                        "retries": request.retries,
                        "failures": request.failures,
                    }
                    for request in provider_requests
                ],
            }

    def snapshot(self) -> MetricsSnapshot:
        """Copy every counter under one lock acquisition, for rendering."""
        with self._lock:
            return MetricsSnapshot(
                healthy=self.is_healthy(),
                uptime_seconds=(datetime.now(timezone.utc) - self.start_time).total_seconds(),
                scan_count=self.scan_count,
                error_count=self.error_count,
                entries_count=self.entries_count,
                available_entries_count=self.available_entries_count,
                available_sites_count=self.available_sites_count,
                last_scan_duration_seconds=self.last_scan_duration_seconds,
                last_scan_timestamp=_timestamp(self.last_scan_time),
                last_alert_scan_timestamp=_timestamp(self.last_alert_scan),
                alert_interval_minutes=self.alert_interval_minutes,
                dashboard_interval_minutes=self.dashboard_interval_minutes,
                last_dashboard_scan_timestamp=_timestamp(self.last_dashboard_scan),
                dashboard_scan_count=self.dashboard_scan_count,
                dashboard_scan_error_count=self.dashboard_scan_error_count,
                dashboard_scan_in_progress=self.dashboard_scan_in_progress,
                last_dashboard_scan_duration_seconds=self.last_dashboard_scan_duration_seconds,
                notifications_sent=self.notifications_sent,
                notifications_failed=self.notifications_failed,
                campgrounds=self.campgrounds,
                provider_throttles=tuple(self.throttle_registry.snapshot()),
                provider_requests=tuple(self.request_metrics.snapshot()),
            )

    def to_prometheus(self) -> str:
        """Render the current status using Prometheus' text exposition format."""
        return render_prometheus(self.snapshot())


scan_status = ScanStatus()
