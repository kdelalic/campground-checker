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
```

No test suite exists in this project.

## Architecture

The project is a thin wrapper around the [camply](https://github.com/juftin/camply) library. The entry point is `check_campsites.py`, which delegates to `campsite_checker/runner.py:main()`.

**Module responsibilities:**

- `campsite_checker/config.py` — CLI argument parsing (`parse_args`), YAML config loading (`load_config`), date range computation, day-of-week filter resolution. Supports two YAML formats: provider-keyed dict (new) and flat list with optional `provider` field (legacy).
- `campsite_checker/providers.py` — Maps provider name strings (`RecreationDotGov`, `Yellowstone`, `GoingToCamp`, `ReserveCalifornia`) to camply search classes; weekday name → integer mapping.
- `campsite_checker/search.py` — Builds camply searcher objects from config entries (`build_searcher`) and runs all searches in parallel via `ThreadPoolExecutor` (`execute_searches`). Handles a provider-specific quirk: `ReserveCalifornia` requires a `recreation_area` positional arg even when only `campground_id` is given, so an empty list is passed in that case.
- `campsite_checker/results.py` — Post-search filtering: excludes boat/hike-in sites, applies day-of-week filter, formats terminal output.
- `campsite_checker/notify.py` — Telegram notification logic: credential resolution (CLI > env vars > YAML), deduplication via `result_keys`, sends HTML-formatted messages via Telegram Bot API.
- `campsite_checker/runner.py` — Orchestrates single-run (`run_once`) and polling loop (`run_forever`). In `--forever` mode, persists sent notification keys to `.campsite_sent_keys.json` to avoid duplicate Telegram alerts across scans.

**Key design decisions:**

- All campground searches run concurrently (one thread per entry).
- Telegram deduplication in `--forever` mode: a key is `(facility_name, campsite_id, booking_date)`. Keys are persisted to disk and pruned of past dates on each load.
- Day-of-week filtering (default: Saturday only) is applied in post-processing, not in the camply search itself.
- Telegram credential priority: CLI flags > `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` env vars > `telegram.bot_token`/`telegram.chat_id` in YAML config.
