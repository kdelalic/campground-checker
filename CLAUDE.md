# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (activate venv first)
source venv/bin/activate
pip install -r requirements.txt

# Run the checker (default: campsites.yaml, next ~6 months, Saturdays only)
python check_campsites.py

# Common options
python check_campsites.py -c my_sites.yaml
python check_campsites.py --start 2026-06-01 --end 2026-08-31
python check_campsites.py --day Friday Saturday
python check_campsites.py --all-days
python check_campsites.py --nights 2
python check_campsites.py --forever --interval 10
python check_campsites.py --verbose   # show camply internal logs
python check_campsites.py --dashboard              # generate dashboard.html
python check_campsites.py --dashboard /tmp/out.html # custom path
python check_campsites.py --no-dashboard           # disable even if YAML enables it
```

No test suite exists in this project.

## Docker

```bash
docker build -t campsite-checker .
docker run -v $(pwd)/campsites.yaml:/app/campsites.yaml campsite-checker
```

The Dockerfile defaults to `python check_campsites.py --forever --dashboard` and uses Python 3.14-slim.

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
- `campsite_checker/search.py` — Builds camply searcher objects from config entries (`build_searcher`) and runs all searches in parallel via `ThreadPoolExecutor` (`execute_searches`). Handles a provider-specific quirk: `ReserveCalifornia` requires a `recreation_area` positional arg even when only `campground_id` is given, so an empty list is passed in that case.
- `campsite_checker/results.py` — Post-search filtering: excludes boat/hike-in sites, applies day-of-week filter, formats terminal output.
- `campsite_checker/notify.py` — Telegram notification logic: credential resolution (CLI > env vars > YAML), deduplication via `result_keys`, per-campground alert filtering, sends HTML-formatted messages via Telegram Bot API.
- `campsite_checker/bot.py` — Telegram bot command handlers (`/list`, `/add`, `/remove`, `/alert`, `/status`, `/help`). `ConfigState` holds thread-safe shared state (entries, raw config, config path) mutated live by bot commands. YAML helper functions (`_append_to_yaml`, `_remove_from_yaml`, `_update_alert_yaml`) persist changes while preserving comments. Only active in `--forever` mode when Telegram credentials are set.
- `campsite_checker/dashboard.py` — Static HTML dashboard generation. Produces a self-contained HTML file with availability tables per campground.
- `campsite_checker/upload.py` — Optional Cloudflare R2 upload for the dashboard. Uses boto3 S3-compatible API. Credentials resolved via env vars or YAML `dashboard.r2` section.
- `campsite_checker/runner.py` — Orchestrates single-run (`run_once`) and polling loop (`run_forever`). In `--forever` mode, individual scan failures are caught and logged without stopping the loop. Persists sent notification keys to `.campsite_sent_keys.json` to avoid duplicate Telegram alerts across scans. Generates dashboard and uploads to R2 after each scan if configured.

**Key design decisions:**

- All campground searches run concurrently (one thread per entry).
- Telegram deduplication in `--forever` mode: a key is `(facility_name, campsite_id, booking_date)`. Keys are persisted to disk and pruned of past dates on each load.
- Day-of-week filtering (default: Saturday only) is applied in post-processing, not in the camply search itself.
- Telegram credential priority: CLI flags > `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` env vars > `telegram.bot_token`/`telegram.chat_id` in YAML config.
- Per-campground alerts: per-entry `alert: true` enables Telegram notifications for that campground (default: off). Terminal output and dashboard still show all sites. Toggleable via `/alert` bot command in `--forever` mode.
- Dashboard: `--dashboard` flag or `dashboard.output_path` in YAML generates a static HTML file after each scan. Optional R2 upload via `dashboard.r2` config or `R2_ACCOUNT_ID`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`/`R2_BUCKET_NAME`/`R2_OBJECT_KEY`/`R2_CUSTOM_DOMAIN` env vars. `boto3` is required only for R2 upload (lazy import).
