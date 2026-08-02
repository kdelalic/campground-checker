# campsite-checker

Check campsite availability across Recreation.gov, ReserveCalifornia, and
ReserveAmerica with native clients, plus Yellowstone and GoingToCamp through
[camply](https://github.com/juftin/camply).

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
    latitude: 37.7361111          # optional dashboard map marker
    longitude: -119.5625          # specify both latitude and longitude
    nights: 2                    # minimum consecutive nights (default: 1)
```

Configured coordinates keep a campground on the map when a successful scan
finds no availability or when a scan fails. When they are omitted, the
dashboard uses provider result coordinates when available.

See [campsites.example.yaml](campsites.example.yaml) for all options and providers.

One campground can search several arrival-day and stay-length combinations.
For example, to check both a Friday-night stay and a Friday-plus-Saturday stay:

```yaml
campsites:
  RecreationDotGov:
    - campground_id: 232491
      alert: true
      criteria:
        - days: [Friday]
          nights: 1
        - days: [Friday]
          nights: 2
```

The dashboard groups matches by arrival date and stay length. When more than
one stay length is configured, its Stay filter narrows campground cards,
calendar counts, result totals, and map markers together. The selection is
kept in the page URL so it survives refreshes and can be shared.

Use `defaults.criteria` to apply the same combinations to every campground:

```yaml
defaults:
  criteria:
    - days: [Friday]
      nights: 1
    - days: [Friday]
      nights: 2
```

An entry with its own `criteria`, `days`, or `nights` schedule overrides these
global criteria.

## Usage

```bash
# Check all campsites — next ~3 months, Sundays only (default)
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
are never cached and are retried on the next scan. ReserveAmerica searches read
the public, server-rendered 14-day availability grid and pace page requests at
one per second; they do not use account credentials or a private API key.
Dashboard files and
R2 objects are only updated when semantic availability changes, plus a
once-per-hour freshness republish so the page's "Last updated" timestamp stays
distinguishable from a stopped checker. Rendering and R2 uploads run on an
isolated, coalescing publisher worker, so a slow upload cannot block alert
polling and only the newest pending snapshot is retained. R2 requests use
bounded timeouts and failed connection pools are discarded; failed uploads are
retried without rebuilding unchanged HTML. Full garbage collection runs every
12 scans by default and logs its duration, object count, and resident-memory
measurement.

## Monitoring

Continuous mode starts an HTTP server on `PORT` (default `8000`):

- `/` returns JSON health and scan status (including Telegram delivery counts
  and bot-thread liveness), with HTTP 503 when scans are stale.
- `/metrics` returns Prometheus text-format counters and gauges for scan health,
  errors, duration, monitored campgrounds, current availability, notification
  delivery, and scan timestamps.

The provided `docker-compose.yml` wires the health endpoint into a container
healthcheck, so a stale checker shows up as an unhealthy container. The canonical
Grafana dashboard is versioned at `grafana/campground-checker.json`.

See [docs/observability.md](docs/observability.md) for the full metric
reference, label semantics, and example PromQL queries.

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

**ReserveAmerica:** The booking URL contains the contract and campground IDs.
For example, `/explore/anthony-chabot/EB/110004/...` maps to:

```yaml
ReserveAmerica:
  - campground_id: 110004
    contract_code: EB
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
