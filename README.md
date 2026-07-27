# campsite-checker

Check campsite availability across Recreation.gov with a native API client, plus
Yellowstone, California State Parks, and GoingToCamp through [camply](https://github.com/juftin/camply).

## Setup

```bash
uv sync --all-extras
```

## Configuration

Copy the example config and fill in your campground IDs:

```bash
cp campsites.example.yaml campsites.yaml
```

Each entry in the `campsites` list needs at minimum a `campground_id` or `recreation_area`:

```yaml
campsites:
  - name: "Yosemite - Upper Pines"
    provider: RecreationDotGov   # default if omitted
    campground_id: 232447
    nights: 2                    # minimum consecutive nights (default: 1)
```

See [campsites.example.yaml](campsites.example.yaml) for all options and providers.

## Usage

```bash
# Check all campsites — next ~6 months, Sundays only (default)
python check_campsites.py

# Use a different config file
python check_campsites.py -c my_campsites.yaml

# Override date range
python check_campsites.py --start 2026-06-01 --end 2026-08-31

# Check specific days
python check_campsites.py --day Friday Saturday

# Check all days of the week
python check_campsites.py --all-days

# Require at least 2 consecutive nights (overrides config)
python check_campsites.py --nights 2

# Show provider search logs
python check_campsites.py --verbose

# Poll continuously every 5 minutes (Ctrl+C to stop)
python check_campsites.py --forever

# Poll every 10 minutes
python check_campsites.py --forever --interval 10
```

## Search Performance Tuning

Compatible campgrounds are searched in bounded batches, and batches run
concurrently. The defaults are four workers, four campgrounds per batch, and no
extra submission delay. In `--forever` mode the worker limit and per-provider
submission delay are enforced process-wide across overlapping alert and
dashboard scans: alert batches are dispatched ahead of queued dashboard
batches, dashboard scans may use at most `workers - 1` slots so alert work
never waits behind a full dashboard queue (with `--workers 1` the single slot
is shared and alert work runs as soon as the in-flight batch finishes), and a
dashboard batch queued for more than a minute is promoted so it cannot starve:

```bash
# More concurrency for a host with sufficient memory and provider headroom
python check_campsites.py --workers 6

# Smaller batches, or disable batching with --batch-size 1
python check_campsites.py --batch-size 2

# Pace requests to each provider independently
python check_campsites.py --search-delay 1

# Disable periodic forced GC, or choose a different scan interval
python check_campsites.py --gc-interval 0
```

Each scan logs per-provider batch counts, median duration, slowest duration, and
total scan time. Use those measurements together with error rates and memory
usage when tuning the values for a deployment.

The container searches alert-enabled campgrounds every minute, refreshes
dashboard-only campgrounds every 10 minutes, and applies a one-second
per-provider batch submission delay. These remain configurable with the
`ALERT_INTERVAL`, `DASHBOARD_INTERVAL`, and `SEARCH_DELAY` environment variables.
Recreation.gov availability is fetched by the native client rather than
Camply. Each request has a 3-second connect timeout and 7-second read timeout and
retries only connection failures, timeouts, and HTTP 5xx responses. It stops
after three attempts with 1- and
2-second delays. HTTP 429 and other 4xx responses are not retried in the request
path. Recreation.gov starts are limited process-wide to three per second with
at most two requests in flight, so overlapping alert and dashboard scans cannot
multiply provider load without bound.

When any provider returns HTTP 429, the checker skips that provider's queued
work and applies an adaptive cooldown. The cooldown starts at 30 seconds,
doubles on consecutive rate limits, honors a longer `Retry-After` response, and
caps the computed exponential backoff at 15 minutes. A successful request begun
after the most recent rate limit resets the backoff streak. Configure the bounds
with `THROTTLE_BASE_DELAY` and `THROTTLE_MAX_DELAY` (seconds).

Resolved Recreation.gov facility identities (campground name and recreation
area) are cached for 24 hours, which removes one RIDB API round trip per
campground from every scan's searcher construction; transient lookup failures
are never cached and are retried on the next scan. Dashboard files and
R2 objects are only updated when semantic availability changes, plus a
once-per-hour freshness republish so the page's "Last updated" timestamp stays
distinguishable from a stopped checker; failed uploads are retried without
rebuilding unchanged HTML. Full garbage collection runs every 12 scans by
default and logs its
duration, object count, and resident-memory measurement.

## Monitoring

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

### Metric reference

Process-wide metrics have no application-defined labels:

| Metric | Type | Description |
| --- | --- | --- |
| `campsite_checker_up` | Gauge | `1` while the checker is warming up or the latest alert scan is less than two alert intervals old; otherwise `0`. |
| `campsite_checker_uptime_seconds` | Gauge | Seconds since the process started. |
| `campsite_checker_scans_total` | Counter | Completed scan cycles, including cycles that ended with an error. |
| `campsite_checker_scan_errors_total` | Counter | Scan cycles that ended with an unhandled error. Individual campground failures are reported by the per-campground success metric. |
| `campsite_checker_campgrounds_monitored` | Gauge | Configured campgrounds included in the latest completed cycle. |
| `campsite_checker_campgrounds_available` | Gauge | Campgrounds with availability in the latest combined alert and dashboard results. |
| `campsite_checker_campsites_available` | Gauge | Available campsite-date combinations in the latest combined results. This is not a count of unique physical sites. |
| `campsite_checker_last_scan_duration_seconds` | Gauge | Wall-clock duration of the latest scan cycle. |
| `campsite_checker_last_scan_timestamp_seconds` | Gauge | Unix timestamp when the latest scan cycle completed, or `0` before the first cycle. |
| `campsite_checker_last_alert_scan_timestamp_seconds` | Gauge | Unix timestamp when the latest priority alert scan completed, or `0` before the first alert scan. |
| `campsite_checker_alert_interval_seconds` | Gauge | Configured interval between alert scans. |
| `campsite_checker_dashboard_interval_seconds` | Gauge | Configured interval between dashboard-only scans. |
| `campsite_checker_last_dashboard_scan_timestamp_seconds` | Gauge | Unix timestamp of the latest dashboard-only scan, or `0` before the first one. |
| `campsite_checker_dashboard_scans_total` | Counter | Completed background dashboard scans. |
| `campsite_checker_dashboard_scan_errors_total` | Counter | Background dashboard scans that ended with an error. |
| `campsite_checker_dashboard_scan_in_progress` | Gauge | `1` while the background dashboard worker is scanning; otherwise `0`. |
| `campsite_checker_last_dashboard_scan_duration_seconds` | Gauge | Duration of the latest completed background dashboard scan. |
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

All three per-campground metrics use the same labels:

| Label | Description |
| --- | --- |
| `config_index` | Position of the entry in `campsites.yaml` (stable across alert-flag changes), keeping otherwise duplicate configured entries as distinct Prometheus series. |
| `provider` | Camply provider, such as `RecreationDotGov` or `ReserveCalifornia`. |
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
when a whole scan cycle fails before results can be assembled. Counters and
timestamps reset when the process restarts.

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

## Telegram Notifications

Get a message when availability is found.
In continuous mode, alert-enabled campgrounds are searched first and new
availability is notified and checkpointed immediately. Dashboard-only scans run
in a separate non-overlapping background worker, so they cannot block the next
priority alert scan.

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID: send your bot a message, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"id"` in `"chat"`.
3. Pass credentials via flags, environment variables, or `campsites.yaml` (pick one):

```bash
# Via CLI flags
python check_campsites.py --telegram-token "123:ABC..." --telegram-chat-id "456789"

# Via environment variables
export TELEGRAM_BOT_TOKEN="123:ABC..."
export TELEGRAM_CHAT_ID="456789"
python check_campsites.py
```

```yaml
# Via campsites.yaml (lowest priority — CLI flags and env vars override this)
telegram:
  bot_token: "123:ABC..."
  chat_id: "456789"

campsites:
  - ...
```

```bash
# Combine with --forever for continuous monitoring with alerts
python check_campsites.py --forever --interval 5
```

In `--forever` mode, Telegram messages are only sent for availability you have
not already been alerted about (deduplicated per provider, campground, site,
and date — persisted across restarts), so you won't get spammed. If a send
fails, the dedup checkpoint is deferred and the alert is retried on the next
scan.

## Finding Campground IDs

**Recreation.gov:** Visit the campground page — the URL contains `facilityId=XXXXX`. Or use camply:

```bash
camply campgrounds --provider RecreationDotGov --search "upper pines"
```

**Yellowstone:**

```bash
camply campgrounds --provider Yellowstone
```

**ReserveCalifornia:**

```bash
camply campgrounds --provider ReserveCalifornia --search "emerald bay"
```

**GoingToCamp (Canadian parks):**

```bash
camply recreation-areas --provider GoingToCamp --search "algonquin"
```

## Example Output

```text
Campsite Availability Check
Date range : 2026-02-16 to 2026-05-18
Days       : Saturday
Campsites  : 2

Searching: Yosemite - Upper Pines ... 5 result(s) found.

============================================================
  Yosemite - Upper Pines
============================================================

  Campground : Upper Pines
  Site       : 042 / A Loop
  Available  : 2 night(s)
    - Sat 2026-04-04
    - Sat 2026-05-09
  Book at    : https://www.recreation.gov/camping/campsites/XXXXX

  Total available nights: 2
```
