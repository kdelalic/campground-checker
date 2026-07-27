# AGENTS.md

This file provides guidance to coding agents working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync --all-extras

# Run the checker (default: campsites.yaml, next ~6 months, Sundays only)
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

CI also runs these checks on every push and PR. The deploy workflow is triggered by a successful CI run on `main` (`workflow_run`), so failing checks block the deploy.

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

The Dockerfile defaults to `python check_campsites.py --forever --dashboard` and uses Python 3.14-slim. The container runs as the unprivileged user `app` (uid 10001); any mounted volume the checker writes (e.g. the `state/` directory) must be writable by that uid on the host. Runtime tunables via env vars: `WORKERS` (default 4), `ALERT_INTERVAL` (default 1), `SEARCH_DELAY` (default 1), `BATCH_SIZE` (default 4), `DASHBOARD_INTERVAL` (default 10), `GC_INTERVAL` (default 12), `THROTTLE_BASE_DELAY` (default 30 seconds), and `THROTTLE_MAX_DELAY` (default 900 seconds). Set `SENT_KEYS_PATH` to persist dedup keys across container restarts (e.g. a mounted volume). `docker-compose.yml` defines a healthcheck against the health endpoint, so a stale checker is reported as an unhealthy container.

## Deployment

Runs on the homelab box at `/srv/docker/campground-checker`, managed by `docker compose`. A successful CI run on `main` triggers `.github/workflows/deploy.yml` (via `workflow_run`), which runs on the self-hosted runner labelled `homelab`; the runner resets that directory to the CI-validated commit, builds the image first (so a broken build never replaces the running container), and then recreates it — the deployment directory, not the runner workdir, is the live checkout.

Credentials (`TELEGRAM_*`, `R2_*`) come from `/srv/docker/campground-checker/.env` (mode 600), not from Actions secrets, so a manual `docker compose up -d` behaves exactly like CI. Telegram dedup keys persist in `state/sent_keys.json` via `SENT_KEYS_PATH`; the container runs as uid 10001, so `state/` must be owned by that uid (one-time `chown -R 10001:10001 state` on the host). Health endpoint: `http://<host>:8000/`.

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
- `campsite_checker/providers.py` — Maps provider name strings (`RecreationDotGov`, `Yellowstone`, `GoingToCamp`, `ReserveCalifornia`) to camply search classes; weekday name → integer mapping. `RecreationDotGov` maps to an identity-caching subclass: resolved facility identity records (`CampgroundFacility`) are held in the process-wide `FACILITY_IDENTITY_CACHE` (24h TTL, LRU-bounded), eliminating the one-RIDB-round-trip-per-campground (~1s each) that stock camply performs during every searcher construction. Failed or filtered lookups are never cached. `ReserveCalifornia` maps to a timeout-enforcing subclass (`TimeoutReserveCalifornia`): stock camply UseDirect HTTP calls have no timeout, so a black-holed connection would hang a scan thread forever; the subclass applies `DEFAULT_HTTP_TIMEOUT_SECONDS` (30s) to every request. `METADATA_PROVIDER_CLASS` exposes the same hardened provider classes for the bot's name lookups.
- `campsite_checker/dispatch.py` — Process-wide `SearchDispatcher` shared by alert and dashboard scans. `--workers` bounds concurrently executing search batches across the whole process; queued alert work is dispatched ahead of queued dashboard work; dashboard work is capped at `workers - 1` concurrent batches (with `workers=1` the single slot is shared: a dashboard batch may occupy it and alert work runs next as soon as it finishes); a dashboard batch that has waited longer than `DASHBOARD_PROMOTION_SECONDS` (60s) is ordered like alert work so sustained alert load cannot starve dashboard scans. `--search-delay` pacing is tracked per provider inside the dispatcher, so overlapping scans share one submission schedule, and provider cooldowns are checked at dispatch time, so a newly activated cooldown fails queued work from both scan types fast (`ProviderCooldownActive`). The module-level dispatcher is created lazily by `execute_searches` and shut down via `shutdown_dispatcher` when the runner exits.
- `campsite_checker/search.py` — Builds camply searcher objects from config entries (`build_searcher`) and runs bounded search batches through the process-wide dispatcher (`execute_searches`). Handles a provider-specific quirk: `ReserveCalifornia` requires a `recreation_area` positional arg even when only `campground_id` is given, so an empty list is passed in that case.
- `campsite_checker/throttle.py` — Detects provider HTTP 429 responses, including exceptions wrapped by retry libraries, and maintains process-wide provider cooldowns. Backoff starts at `THROTTLE_BASE_DELAY`, doubles on consecutive limits, honors longer `Retry-After` values, and caps the computed exponential delay at `THROTTLE_MAX_DELAY`. Queued work for a throttled provider is skipped until a later scan while other providers continue.
- `campsite_checker/results.py` — Post-search filtering: excludes boat/hike-in sites, applies day-of-week filter, formats terminal output.
- `campsite_checker/notify.py` — Telegram notification logic: credential resolution (CLI > env vars > YAML), dedup filtering via `filter_new_availability`, per-campground alert filtering, sends HTML-formatted messages via Telegram Bot API. `send_telegram` returns delivery success so the runner can defer the dedup checkpoint when a send fails (the alert is retried next scan instead of being lost).
- `campsite_checker/yaml_editor.py` — Read-only YAML comment parsing via `ruamel.yaml`. `parse_yaml_comments` recovers campground names from the inline comments in `campsites.yaml` for `bot.py` and `config.py`. The old mutating helpers were removed: `campsites.yaml` is git-managed and mounted read-only, so nothing may write it at runtime.
- `campsite_checker/bot.py` — Telegram bot command handlers (`/list`, `/alert`, `/status`, `/help`). Read-only: the bot reports what is being monitored but never edits `campsites.yaml`. `ConfigState` holds thread-safe shared state (entries, raw config, config path); the lock is held only for in-memory reads, with network calls made outside it. Only active in `--forever` mode when Telegram credentials are set.
- `campsite_checker/dashboard.py` — Static HTML dashboard generation. Produces a self-contained HTML file with availability tables per campground.
- `campsite_checker/upload.py` — Optional Cloudflare R2 upload for the dashboard. Uses boto3 S3-compatible API. Credentials resolved via env vars or YAML `dashboard.r2` section.
- `campsite_checker/server.py` — Health check HTTP server. Returns JSON with scan count, error count, last scan time, uptime. Returns 503 if the scanner appears stuck. Runs on `PORT` env var (default 8000).
- `campsite_checker/runner.py` — Orchestrates single-run (`run_once`) and polling loop (`run_forever`). In `--forever` mode, uses two-tier scanning: entries with `alert: true` are searched every `--alert-interval` minutes (CLI default 5; container default 1), while dashboard-only entries are searched every `--dashboard-interval` minutes (CLI default 60; container default 10; configurable via CLI, `dashboard.interval` YAML key, or `DASHBOARD_INTERVAL` env var). Alert scans, notifications, and their dedup checkpoint run in the foreground. Dashboard-only scans run in one non-overlapping background worker, and their completed results are merged into the next foreground cycle without blocking the alert deadline. Individual scan failures are caught and logged without stopping the loop. Persists sent notification keys to `SENT_KEYS_PATH` (default `.campsite_sent_keys.json`) to avoid duplicate Telegram alerts across scans.
- `grafana/campground-checker.json` — Canonical provisioned Grafana dashboard for the Prometheus metrics exposed by `server.py`. The deploy workflow copies it into the homelab monitoring stack; update it in the same change as any metric rename or removal.

**Key design decisions:**

- Compatible campground searches are grouped into bounded batches; batches from all overlapping scans run concurrently under one process-wide worker limit with global per-provider submission pacing (see `dispatch.py`).
- Telegram deduplication in `--forever` mode: a key is `(provider, entry_identity, campsite_id, booking_date)`, where `entry_identity` is the configured campground/recreation-area ID — a provider-side facility rename cannot re-trigger or suppress alerts. Keys are persisted atomically to disk, kept for the full search horizon, and pruned of past dates. Keys are only checkpointed after all Telegram messages for the cycle were delivered, so a failed send is retried next scan.
- Day-of-week filtering (default: Sunday only) is applied in post-processing, not in the camply search itself.
- Telegram credential priority: CLI flags > `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` env vars > `telegram.bot_token`/`telegram.chat_id` in YAML config.
- Per-campground alerts: per-entry `alert: true` enables Telegram notifications for that campground (default: off). Terminal output and dashboard still show all sites. Change it by editing `campsites.yaml` and pushing; `/alert` in the bot displays current state but cannot change it.
- `campsites.yaml` is the source of truth and lives in git. The deployment mounts it read-only and every deploy resets the working tree, so runtime edits would not survive regardless.
- Dashboard: `--dashboard` flag or `dashboard.output_path` in YAML generates a static HTML file after each scan. Optional R2 upload via `dashboard.r2` config or `R2_ACCOUNT_ID`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`/`R2_BUCKET_NAME`/`R2_OBJECT_KEY`/`R2_CUSTOM_DOMAIN` env vars. `boto3` is required only for R2 upload (lazy import).
