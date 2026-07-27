# AGENTS.md

This file provides guidance to coding agents working with code in this repository.

The project is a campsite availability checker: a thin wrapper around
[camply](https://github.com/juftin/camply) plus a native Recreation.gov client,
which polls providers, sends Telegram alerts, and renders a static dashboard.

## Where things are documented

| Topic | File |
| --- | --- |
| Module map, design decisions | [docs/architecture.md](docs/architecture.md) |
| Metric reference, Grafana, PromQL | [docs/observability.md](docs/observability.md) |
| Docker, env vars, homelab deploy | [docs/deployment.md](docs/deployment.md) |
| User-facing setup and usage | [README.md](README.md) |
| Config format and all options | [campsites.example.yaml](campsites.example.yaml) |

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

Each `campsite_checker/<module>.py` has a matching `tests/test_<module>.py`.
Keep that pairing when adding or splitting modules.

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

Any metric addition, rename, removal, label change, or semantic change must
update `grafana/campground-checker.json`, the reference in
[docs/observability.md](docs/observability.md), and
`tests/test_grafana_dashboard.py` in the same change. Dashboard panels must not
reference metrics that the `/metrics` endpoint does not export.

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
