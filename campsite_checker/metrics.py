"""Prometheus text-exposition rendering for the `/metrics` endpoint.

`ScanStatus` (see `status.py`) owns the live counters; this module owns their
names, help strings, and wire format. Keep any change here in sync with the
metric reference in `docs/observability.md` and `grafana/campground-checker.json`.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime

from .request_gate import ProviderRequestSnapshot
from .throttle import ProviderThrottleSnapshot


class CampgroundScanFailures:
    """Cumulative failed searches per campground, keyed by stable config index.

    `campsite_checker_campground_last_scan_success` is a gauge, so a campground
    that fails intermittently is indistinguishable from a healthy one unless
    you happen to scrape mid-failure. This counter makes that history
    rate-able, and it is the only signal that moves for a per-campground
    failure: `scan_errors_total` counts whole-scan aborts, so a scan in which
    some campgrounds failed and others succeeded leaves it at zero.

    Incremented once per real search attempt from `search.execute_searches`
    rather than from `ScanStatus.update`, because every alert scan re-publishes
    the cached dashboard results and would otherwise re-count one dashboard
    failure on each alert cycle.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[int, int] = {}

    def record_failure(self, config_index: int) -> None:
        with self._lock:
            self._counts[config_index] = self._counts.get(config_index, 0) + 1

    def get(self, config_index: int) -> int:
        with self._lock:
            return self._counts.get(config_index, 0)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


CAMPGROUND_SCAN_FAILURES = CampgroundScanFailures()


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
    scan_failures: int = 0

    @classmethod
    def from_entry(
        cls,
        entry: dict,
        *,
        config_index: int,
        available: bool,
        available_sites: int,
        scan_success: bool = True,
        scan_failures: int | None = None,
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
            scan_failures=(
                CAMPGROUND_SCAN_FAILURES.get(config_index)
                if scan_failures is None
                else scan_failures
            ),
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


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Immutable copy of the scan counters, taken under the status lock.

    Rendering reads ~20 fields; snapshotting them together keeps the lock hold
    short and keeps this module free of `ScanStatus` internals.
    """

    healthy: bool
    uptime_seconds: float
    scan_count: int
    error_count: int
    entries_count: int
    # None means "no complete result snapshot yet", which renders as an absent
    # gauge instead of a misleading 0; see `ScanStatus.snapshot`.
    available_entries_count: int | None
    available_sites_count: int | None
    last_scan_duration_seconds: float
    last_scan_timestamp: float
    last_alert_scan_timestamp: float
    alert_interval_minutes: int
    dashboard_interval_minutes: int
    last_dashboard_scan_timestamp: float
    dashboard_scan_count: int
    dashboard_scan_error_count: int
    dashboard_scan_in_progress: bool
    last_dashboard_scan_duration_seconds: float
    notifications_sent: int
    notifications_failed: int
    campgrounds: tuple[CampgroundMetric, ...] = ()
    provider_throttles: tuple[ProviderThrottleSnapshot, ...] = field(default_factory=tuple)
    provider_requests: tuple[ProviderRequestSnapshot, ...] = field(default_factory=tuple)


def _timestamp(value: datetime | None) -> float:
    """Unix timestamp, or 0 for "never happened" (Prometheus has no null)."""
    return value.timestamp() if value else 0


def render_prometheus(snapshot: MetricsSnapshot) -> str:
    """Render a snapshot using Prometheus' text exposition format."""
    process_metrics = (
        (
            "campsite_checker_up",
            "Whether the campsite checker is healthy (1) or stale (0).",
            "gauge",
            int(snapshot.healthy),
        ),
        (
            "campsite_checker_uptime_seconds",
            "Seconds since the campsite checker process started.",
            "gauge",
            snapshot.uptime_seconds,
        ),
        (
            "campsite_checker_scans_total",
            "Total number of completed scan cycles.",
            "counter",
            snapshot.scan_count,
        ),
        (
            "campsite_checker_scan_errors_total",
            "Total number of scan cycles that ended with an error.",
            "counter",
            snapshot.error_count,
        ),
        (
            "campsite_checker_campgrounds_monitored",
            "Number of campgrounds in the most recent scan cycle.",
            "gauge",
            snapshot.entries_count,
        ),
        (
            "campsite_checker_campgrounds_available",
            "Number of campgrounds with availability in the latest results.",
            "gauge",
            snapshot.available_entries_count,
        ),
        (
            "campsite_checker_campsites_available",
            "Number of available campsite-date combinations in the latest results.",
            "gauge",
            snapshot.available_sites_count,
        ),
        (
            "campsite_checker_last_scan_duration_seconds",
            "Duration of the most recent scan cycle in seconds.",
            "gauge",
            snapshot.last_scan_duration_seconds,
        ),
        (
            "campsite_checker_last_scan_timestamp_seconds",
            "Unix timestamp of the most recent completed scan cycle.",
            "gauge",
            snapshot.last_scan_timestamp,
        ),
        (
            "campsite_checker_last_alert_scan_timestamp_seconds",
            "Unix timestamp of the most recent completed alert scan.",
            "gauge",
            snapshot.last_alert_scan_timestamp,
        ),
        (
            "campsite_checker_alert_interval_seconds",
            "Configured interval between alert scan cycles in seconds.",
            "gauge",
            snapshot.alert_interval_minutes * 60,
        ),
        (
            "campsite_checker_dashboard_interval_seconds",
            "Configured interval between dashboard scans in seconds.",
            "gauge",
            snapshot.dashboard_interval_minutes * 60,
        ),
        (
            "campsite_checker_last_dashboard_scan_timestamp_seconds",
            "Unix timestamp of the most recent dashboard scan.",
            "gauge",
            snapshot.last_dashboard_scan_timestamp,
        ),
        (
            "campsite_checker_dashboard_scans_total",
            "Total number of completed background dashboard scans.",
            "counter",
            snapshot.dashboard_scan_count,
        ),
        (
            "campsite_checker_dashboard_scan_errors_total",
            "Total number of background dashboard scans that ended with an error.",
            "counter",
            snapshot.dashboard_scan_error_count,
        ),
        (
            "campsite_checker_dashboard_scan_in_progress",
            "Whether a background dashboard scan is currently running.",
            "gauge",
            int(snapshot.dashboard_scan_in_progress),
        ),
        (
            "campsite_checker_last_dashboard_scan_duration_seconds",
            "Duration of the most recent background dashboard scan in seconds.",
            "gauge",
            snapshot.last_dashboard_scan_duration_seconds,
        ),
        (
            "campsite_checker_notifications_sent_total",
            "Total number of Telegram alert messages delivered successfully.",
            "counter",
            snapshot.notifications_sent,
        ),
        (
            "campsite_checker_notifications_failed_total",
            "Total number of Telegram alert messages that failed to send.",
            "counter",
            snapshot.notifications_failed,
        ),
    )

    lines = []
    for name, help_text, metric_type, value in process_metrics:
        # A None value means the metric has nothing meaningful to report yet;
        # omit the series entirely so consumers see a gap rather than a zero.
        if value is None:
            continue
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
            f"{name}{campground.labels()} {get_value(campground)}"
            for campground in snapshot.campgrounds
        )

    failures_metric = "campsite_checker_campground_scan_failures_total"
    lines.extend(
        (
            f"# HELP {failures_metric} Failed searches for the configured campground.",
            f"# TYPE {failures_metric} counter",
        )
    )
    lines.extend(
        f"{failures_metric}{campground.labels()} {campground.scan_failures}"
        for campground in snapshot.campgrounds
    )

    throttle_metrics = (
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
    for name, help_text, metric_type, get_value in throttle_metrics:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))
        lines.extend(
            f'{name}{{provider="{_escape_label_value(throttle.provider)}"}} {get_value(throttle)}'
            for throttle in snapshot.provider_throttles
        )

    request_metrics = (
        (
            "campsite_checker_provider_request_attempts_total",
            "HTTP request attempts made by the native provider client.",
            lambda request: request.attempts,
        ),
        (
            "campsite_checker_provider_request_retries_total",
            "HTTP request retries made by the native provider client.",
            lambda request: request.retries,
        ),
        (
            "campsite_checker_provider_request_failures_total",
            "HTTP requests that exhausted or bypassed native provider retries.",
            lambda request: request.failures,
        ),
    )
    for name, help_text, get_value in request_metrics:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} counter"))
        lines.extend(
            f'{name}{{provider="{_escape_label_value(request.provider)}"}} {get_value(request)}'
            for request in snapshot.provider_requests
        )

    return "\n".join(lines) + "\n"
