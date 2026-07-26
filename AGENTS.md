# AGENTS.md

This file provides guidance to coding agents working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync --all-extras

# Run the checker (default: campsites.yaml, next ~6 months, Saturdays only)
uv run python check_campsites.py

# Common options
uv run python check_campsites.py -c my_sites.yaml
uv run python check_campsites.py --start 2026-06-01 --end 2026-08-31
uv run python check_campsites.py --day Friday Saturday
uv run python check_campsites.py --all-days
uv run python check_campsites.py --nights 2
uv run python check_campsites.py --forever --alert-interval 10
uv run python check_campsites.py --workers 2 --search-delay 1  # tune for low-CPU environments
uv run python check_campsites.py --verbose   # show camply internal logs
uv run python check_campsites.py --dashboard              # generate dashboard.html
uv run python check_campsites.py --dashboard /tmp/out.html # custom path
uv run python check_campsites.py --no-dashboard           # disable even if YAML enables it
uv run python check_campsites.py --dashboard-interval 10  # dashboard-only sites scraped every 10 min
```

## Testing & Linting

```bash
# Install dependencies (includes dev extras)
uv sync --all-extras

# Run tests
uv run pytest -v

# Run a single test file
uv run pytest tests/test_config.py -v

# Run linting
uv run ruff check .

# Check formatting (does not modify files)
uv run ruff format --check .

# Auto-fix lint issues
uv run ruff check . --fix

# Auto-format code
uv run ruff format .
```

### Pre-Push Checklist

Before committing and pushing changes, **always**:

1. Run `uv run ruff check .` and fix any issues
2. Run `uv run ruff format .` to ensure consistent formatting
3. Run `uv run pytest -v` and ensure all tests pass
4. Do not push if any test fails or lint error remains

```bash
# Quick pre-push command:
uv run ruff check . && uv run ruff format --check . && uv run pytest -v
```

CI will also run these checks on every push and PR. Failing checks will block the deploy workflow.

## Observability Changes

Keep Prometheus metrics, the metric reference in `README.md`, and
`grafana/campground-checker.json` in sync. Any metric addition, rename, removal,
label change, or semantic change must update the Grafana dashboard and
`tests/test_grafana_dashboard.py` in the same change. Dashboard panels must not
reference metrics that the `/metrics` endpoint does not export.

## Docker

```bash
docker build -t campsite-checker .
docker run -v $(pwd)/campsites.yaml:/app/campsites.yaml:ro campsite-checker
```

The Dockerfile defaults to `python check_campsites.py --forever --dashboard` and uses Python 3.14-slim. Runtime tunables via env vars: `WORKERS` (default 4), `ALERT_INTERVAL` (default 1), `SEARCH_DELAY` (default 1), `BATCH_SIZE` (default 4), `DASHBOARD_INTERVAL` (default 10), `GC_INTERVAL` (default 12), `THROTTLE_BASE_DELAY` (default 30 seconds), and `THROTTLE_MAX_DELAY` (default 900 seconds). Set `SENT_KEYS_PATH` to persist dedup keys across container restarts (e.g. a mounted volume).

## Deployment

Runs on the homelab box at `/srv/docker/campground-checker`, managed by `docker compose`. Pushing to `main` triggers `.github/workflows/deploy.yml`, which runs on the self-hosted runner labelled `homelab`; the runner does a `git reset --hard origin/main` in that directory and recreates the container, so the deployment directory — not the runner workdir — is the live checkout.

Credentials (`TELEGRAM_*`, `R2_*`) come from `/srv/docker/campground-checker/.env` (mode 600), not from Actions secrets, so a manual `docker compose up -d` behaves exactly like CI. Telegram dedup keys persist in `state/sent_keys.json` via `SENT_KEYS_PATH`. Health endpoint: `http://<host>:8000/`.

## Disabling Campgrounds

Any campground entry can be disabled by adding `enabled: false`. Disabled entries are skipped at config load time, so they don't appear in searches, the dashboard, or bot commands. To re-enable, just remove the `enabled: false` line or set it to `true`.

## Finding Campground IDs

```bash
camply campgrounds --provider RecreationDotGov --search "upper pines"
camply campgrounds --provider Yellowstone
camply campgrounds --provider ReserveCalifornia --search "emerald bay"
camply recreation-areas --provider GoingToCamp --search "algonquin"
```

For Recreation.gov, the campground page URL also contains `facilityId=XXXXX`.

## Architecture

The project is a thin wrapper around the [camply](https://github.com/juftin/camply) library. The entry point is `check_campsites.py`, which delegates to `campsite_checker/runner.py:main()`.

**Module responsibilities:**

- `campsite_checker/config.py` — CLI argument parsing (`parse_args`), YAML config loading (`load_config`), date range computation, day-of-week filter resolution. Supports two YAML formats: provider-keyed dict (new) and flat list with optional `provider` field (legacy).
- `campsite_checker/providers.py` — Maps provider name strings (`RecreationDotGov`, `Yellowstone`, `GoingToCamp`, `ReserveCalifornia`) to camply search classes; weekday name → integer mapping.
- `campsite_checker/search.py` — Builds camply searcher objects from config entries (`build_searcher`) and runs bounded search batches in parallel via `ThreadPoolExecutor` (`execute_searches`). Batch submissions are paced independently per provider. Handles a provider-specific quirk: `ReserveCalifornia` requires a `recreation_area` positional arg even when only `campground_id` is given, so an empty list is passed in that case.
- `campsite_checker/throttle.py` — Detects provider HTTP 429 responses, including exceptions wrapped by retry libraries, and maintains process-wide provider cooldowns. Backoff starts at `THROTTLE_BASE_DELAY`, doubles on consecutive limits, honors longer `Retry-After` values, and caps the computed exponential delay at `THROTTLE_MAX_DELAY`. Queued work for a throttled provider is skipped until a later scan while other providers continue.
- `campsite_checker/results.py` — Post-search filtering: excludes boat/hike-in sites, applies day-of-week filter, formats terminal output.
- `campsite_checker/notify.py` — Telegram notification logic: credential resolution (CLI > env vars > YAML), deduplication via `result_keys`, per-campground alert filtering, sends HTML-formatted messages via Telegram Bot API.
- `campsite_checker/yaml_editor.py` — Round-trip YAML handling via `ruamel.yaml`. `parse_yaml_comments` is the only function still called; it recovers campground names from the inline comments in `campsites.yaml` for `bot.py`. The mutating helpers (`append_campground`, `remove_campground`, `update_campground_comment`, `update_alert_field`) are retained and still tested, but nothing calls them now that `campsites.yaml` is git-managed and mounted read-only.
- `campsite_checker/bot.py` — Telegram bot command handlers (`/list`, `/alert`, `/status`, `/help`). Read-only: the bot reports what is being monitored but never edits `campsites.yaml`. `ConfigState` holds thread-safe shared state (entries, raw config, config path); the lock is held only for in-memory reads, with network calls made outside it. Only active in `--forever` mode when Telegram credentials are set.
- `campsite_checker/dashboard.py` — Static HTML dashboard generation. Produces a self-contained HTML file with availability tables per campground.
- `campsite_checker/upload.py` — Optional Cloudflare R2 upload for the dashboard. Uses boto3 S3-compatible API. Credentials resolved via env vars or YAML `dashboard.r2` section.
- `campsite_checker/server.py` — Health check HTTP server. Returns JSON with scan count, error count, last scan time, uptime. Returns 503 if the scanner appears stuck. Runs on `PORT` env var (default 8000).
- `campsite_checker/runner.py` — Orchestrates single-run (`run_once`) and polling loop (`run_forever`). In `--forever` mode, uses two-tier scanning: entries with `alert: true` are searched every `--alert-interval` minutes (CLI default 5; container default 1), while dashboard-only entries are searched every `--dashboard-interval` minutes (CLI default 60; container default 10; configurable via CLI, `dashboard.interval` YAML key, or `DASHBOARD_INTERVAL` env var). Alert scans, notifications, and their dedup checkpoint run in the foreground. Dashboard-only scans run in one non-overlapping background worker, and their completed results are merged into the next foreground cycle without blocking the alert deadline. Individual scan failures are caught and logged without stopping the loop. Persists sent notification keys to `SENT_KEYS_PATH` (default `.campsite_sent_keys.json`) to avoid duplicate Telegram alerts across scans.
- `grafana/campground-checker.json` — Canonical provisioned Grafana dashboard for the Prometheus metrics exposed by `server.py`. The deploy workflow copies it into the homelab monitoring stack; update it in the same change as any metric rename or removal.

**Key design decisions:**

- Compatible campground searches are grouped into bounded batches; batches run concurrently with per-provider submission pacing.
- Telegram deduplication in `--forever` mode: a key is `(facility_name, campsite_id, booking_date)`. Keys are persisted to disk and pruned of past dates on each load.
- Day-of-week filtering (default: Saturday only) is applied in post-processing, not in the camply search itself.
- Telegram credential priority: CLI flags > `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` env vars > `telegram.bot_token`/`telegram.chat_id` in YAML config.
- Per-campground alerts: per-entry `alert: true` enables Telegram notifications for that campground (default: off). Terminal output and dashboard still show all sites. Change it by editing `campsites.yaml` and pushing; `/alert` in the bot displays current state but cannot change it.
- `campsites.yaml` is the source of truth and lives in git. The deployment mounts it read-only and every deploy resets the working tree, so runtime edits would not survive regardless.
- Dashboard: `--dashboard` flag or `dashboard.output_path` in YAML generates a static HTML file after each scan. Optional R2 upload via `dashboard.r2` config or `R2_ACCOUNT_ID`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`/`R2_BUCKET_NAME`/`R2_OBJECT_KEY`/`R2_CUSTOM_DOMAIN` env vars. `boto3` is required only for R2 upload (lazy import).
