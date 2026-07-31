# Observability

Metrics are defined in [campsite_checker/metrics.py](../campsite_checker/metrics.py)
and fed by the counters in [campsite_checker/status.py](../campsite_checker/status.py),
which [campsite_checker/server.py](../campsite_checker/server.py) exposes over HTTP.

## Changing metrics

Keep the Prometheus metrics, the reference below, and
`grafana/campground-checker.json` in sync. Any metric addition, rename, removal,
label change, or semantic change must update the Grafana dashboard and
`tests/test_grafana_dashboard.py` in the same change. Dashboard panels must not
reference metrics that the `/metrics` endpoint does not export.

## Endpoints

Continuous mode starts an HTTP server on `PORT` (default `8000`):

- `/` returns JSON health and scan status (including Telegram delivery counts
  and bot-thread liveness), with HTTP 503 when scans are stale.
- `/metrics` returns Prometheus text-format counters and gauges for scan health,
  errors, duration, monitored campgrounds, current availability, notification
  delivery, and scan timestamps.

The provided `docker-compose.yml` wires this endpoint into a container
healthcheck, so a stale checker shows up as an unhealthy container.

Each configured campground also gets labeled
`campsite_checker_campground_available` and
`campsite_checker_campground_campsites_available` gauges, plus
`campsite_checker_campground_last_scan_success` to distinguish no availability
from a provider error. Labels include the provider, campground/recreation-area/site
IDs, configured name, alert status, and configuration index. Metrics stay at
campground granularity rather than adding booking-date labels, which keeps
Prometheus cardinality bounded.

## Metric reference

Process-wide metrics have no application-defined labels:

| Metric | Type | Description |
| --- | --- | --- |
| `campsite_checker_up` | Gauge | `1` while the checker is warming up or the latest alert scan is less than two alert intervals old; otherwise `0`. |
| `campsite_checker_uptime_seconds` | Gauge | Seconds since the process started. |
| `campsite_checker_scans_total` | Counter | Completed scan cycles, including cycles that ended with an error. |
| `campsite_checker_scan_errors_total` | Counter | Scan cycles that ended with an unhandled error. Individual campground failures are reported by the per-campground success metric. |
| `campsite_checker_campgrounds_monitored` | Gauge | Configured campgrounds included in the latest completed cycle. |
| `campsite_checker_campgrounds_available` | Gauge | Campgrounds with availability in the latest combined alert and dashboard results. Absent until the first complete snapshot. |
| `campsite_checker_campsites_available` | Gauge | Available campsite-date combinations in the latest combined results. This is not a count of unique physical sites. Absent until the first complete snapshot. |
| `campsite_checker_last_scan_duration_seconds` | Gauge | Wall-clock duration of the latest foreground alert cycle, excluding asynchronous dashboard publication. Retained for compatibility. |
| `campsite_checker_last_alert_scan_duration_seconds` | Gauge | Wall-clock duration of the latest priority alert search, recorded before Telegram delivery and dashboard work. |
| `campsite_checker_last_scan_timestamp_seconds` | Gauge | Unix timestamp when the latest scan cycle completed, or `0` before the first cycle. |
| `campsite_checker_last_alert_scan_timestamp_seconds` | Gauge | Unix timestamp when the latest priority alert scan completed, or `0` before the first alert scan. |
| `campsite_checker_alert_interval_seconds` | Gauge | Configured interval between alert scans. |
| `campsite_checker_dashboard_interval_seconds` | Gauge | Configured interval between dashboard-only scans. |
| `campsite_checker_last_dashboard_scan_timestamp_seconds` | Gauge | Unix timestamp of the latest dashboard-only scan, or `0` before the first one. |
| `campsite_checker_dashboard_scans_total` | Counter | Completed background dashboard scans. |
| `campsite_checker_dashboard_scan_errors_total` | Counter | Background dashboard scans that ended with an error. |
| `campsite_checker_dashboard_scan_in_progress` | Gauge | `1` while the background dashboard worker is scanning; otherwise `0`. |
| `campsite_checker_last_dashboard_scan_duration_seconds` | Gauge | Duration of the latest completed background dashboard scan. |
| `campsite_checker_last_dashboard_publish_timestamp_seconds` | Gauge | Unix timestamp of the latest completed asynchronous dashboard publication cycle. |
| `campsite_checker_dashboard_publishes_total` | Counter | Completed asynchronous dashboard publication cycles, including no-op fingerprint checks. |
| `campsite_checker_dashboard_publish_errors_total` | Counter | Publication cycles that failed to render or whose attempted R2 upload failed. |
| `campsite_checker_dashboard_publish_in_progress` | Gauge | `1` while the isolated publisher is rendering or uploading; otherwise `0`. |
| `campsite_checker_last_dashboard_publish_duration_seconds` | Gauge | Total render/upload duration of the latest publication cycle. |
| `campsite_checker_last_dashboard_render_duration_seconds` | Gauge | Duration of the latest HTML render; retains the previous value when a fingerprint check skips rendering. |
| `campsite_checker_r2_uploads_total` | Counter | Attempted Cloudflare R2 dashboard uploads. |
| `campsite_checker_r2_upload_failures_total` | Counter | Failed Cloudflare R2 dashboard uploads. |
| `campsite_checker_last_r2_upload_duration_seconds` | Gauge | Duration of the latest R2 upload attempt; retains the previous value when no upload is needed. |
| `campsite_checker_notifications_sent_total` | Counter | Telegram alert messages delivered successfully. |
| `campsite_checker_notifications_failed_total` | Counter | Telegram alert messages that failed to send. Failed sends defer the dedup checkpoint, so the availability is retried on the next scan instead of being lost. |

These metrics are emitted once per provider, using the `provider` label:

| Metric | Type | Description |
| --- | --- | --- |
| `campsite_checker_provider_rate_limit_events_total` | Counter | Rate-limit responses handed to the adaptive provider cooldown. |
| `campsite_checker_provider_throttle_cooldown_seconds` | Gauge | Seconds remaining in the provider's adaptive cooldown. |
| `campsite_checker_provider_throttle_last_backoff_seconds` | Gauge | Most recent cooldown applied to the provider. |
| `campsite_checker_provider_consecutive_rate_limits` | Gauge | Consecutive rate limits without a subsequent successful request. |
| `campsite_checker_provider_request_attempts_total` | Counter | Native provider HTTP request attempts, including retries. |
| `campsite_checker_provider_request_retries_total` | Counter | Native provider HTTP retries after connection failures, timeouts, or 5xx responses. |
| `campsite_checker_provider_request_failures_total` | Counter | Native provider HTTP requests that failed without a retry or exhausted all attempts. |

These metrics are emitted once per configured campground:

| Metric | Type | Description |
| --- | --- | --- |
| `campsite_checker_campground_available` | Gauge | `1` when the campground's latest results contain availability; otherwise `0`. |
| `campsite_checker_campground_campsites_available` | Gauge | Available campsite-date combinations for the campground. |
| `campsite_checker_campground_last_scan_success` | Gauge | `1` when every search task for the campground succeeded; `0` when at least one provider search failed. |
| `campsite_checker_campground_scan_failures_total` | Counter | Cumulative failed searches for the campground. The success gauge above only shows the latest scan, so this is what makes an intermittently failing campground visible over a range. |

All four per-campground metrics use the same labels:

| Label | Description |
| --- | --- |
| `config_index` | Position of the entry in `campsites.yaml` (stable across alert-flag changes), keeping otherwise duplicate configured entries as distinct Prometheus series. |
| `provider` | Provider key, such as `RecreationDotGov`, `ReserveCalifornia`, or `ReserveAmerica`. |
| `campground_id` | Configured campground ID, or an empty string when unused. Lists are comma-separated. |
| `recreation_area` | Configured recreation-area ID, or an empty string when unused. Lists are comma-separated. |
| `campsite_id` | Optional configured site filter, not the IDs of every currently available site. |
| `name` | Configured `name`; falls back to the campground, recreation-area, or campsite ID. |
| `alert` | `true` for alert-tier entries and `false` for dashboard-only entries. |

Alert-tier campground metrics refresh every alert cycle. Dashboard-only metrics
retain their last results and refresh on the dashboard interval. Treat
`campsite_checker_campground_available == 0` as confirmed no availability only
when the matching `campsite_checker_campground_last_scan_success` is `1`.
Per-campground series are absent before the first complete result snapshot or
when a whole scan cycle fails before results can be assembled. The two
aggregate availability gauges (`campsite_checker_campgrounds_available` and
`campsite_checker_campsites_available`) are absent for the same reason: alert
scans publish before the first dashboard sweep finishes, so until then their
totals would cover only the alert tier and understate the real figures.
Exporting that as `0` would graph a drop indistinguishable from "availability
vanished". `campsite_checker_campgrounds_monitored` is exempt — it is the
configured entry count and is correct immediately.

Counters and timestamps reset when the process restarts. Restarts are routine
here: every deploy, plus the daily state backup that briefly stops the
container. Use `rate()` or `increase()` for anything spanning a restart, since
a raw counter value only covers the current process lifetime. Timestamp metrics
report `0` for "never happened" (Prometheus has no null), so guard staleness
queries with `> 0` — for example
`time() - (campsite_checker_last_alert_scan_timestamp_seconds > 0)` — or a
freshly started process reads as infinitely stale.

Example Prometheus scrape configuration:

```yaml
scrape_configs:
  - job_name: campsite-checker
    static_configs:
      - targets: ["campsite-checker:8000"]
```

The canonical Grafana dashboard is versioned at
`grafana/campground-checker.json`. Homelab deployments copy it into Grafana's
provisioned dashboard directory, so metric renames or removals should update the
dashboard in the same pull request. The test suite verifies that every
`campsite_checker_*` metric referenced by the dashboard is still exported. Its
per-campground panels provide a compact availability table, a failed-search
summary and error list, and an availability-events timeline, with provider and
alert-tier filters.

Useful PromQL queries:

```promql
# Unhealthy or stale checker
campsite_checker_up == 0

# Campground provider searches that failed
campsite_checker_campground_last_scan_success == 0

# Share of the last day each campground spent in a successful-scan state.
# `scan_errors_total` stays at zero when some campgrounds fail and the
# surrounding scan cycle still completes, so prefer this for coverage.
avg_over_time(campsite_checker_campground_last_scan_success[24h])

# Campgrounds failing most often over the last hour
topk(5, increase(campsite_checker_campground_scan_failures_total[1h]))

# Campgrounds with confirmed availability
campsite_checker_campground_available == 1
and campsite_checker_campground_last_scan_success == 1

# Number of available campgrounds by provider
sum by (provider) (campsite_checker_campground_available == 1)

# Alert scan has not completed within two configured intervals
time() - campsite_checker_last_alert_scan_timestamp_seconds
  > 2 * campsite_checker_alert_interval_seconds

# Rate of whole-cycle errors over the last hour
rate(campsite_checker_scan_errors_total[1h])

# Providers currently in an adaptive cooldown
campsite_checker_provider_throttle_cooldown_seconds > 0

# Provider rate-limit responses observed in the last hour
increase(campsite_checker_provider_rate_limit_events_total[1h]) > 0

# Telegram alert deliveries that failed in the last hour
increase(campsite_checker_notifications_failed_total[1h]) > 0
```
