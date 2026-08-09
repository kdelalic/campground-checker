# AGENTS.md

This file provides guidance to coding agents working with code in this repository.

The project is a campsite availability checker: a thin wrapper around
[camply](https://github.com/juftin/camply) plus a native Recreation.gov client,
which polls providers, sends Telegram alerts, and renders a static dashboard.

## Where things are documented

| Topic | File |
| --- | --- |
| Module map, design decisions | [docs/architecture.md](docs/architecture.md) |
| Dashboard visual language and UI conventions | [docs/dashboard-style-guide.md](docs/dashboard-style-guide.md) |
| Metric reference, Grafana, PromQL | [docs/observability.md](docs/observability.md) |
| Docker, env vars, homelab deploy | [docs/deployment.md](docs/deployment.md) |
| User-facing setup and usage | [README.md](README.md) |
| Config format and all options | [campsites.example.yaml](campsites.example.yaml) |

## Commands

```bash
# Install dependencies
uv sync --all-extras

# Run the checker (default: campsites.yaml, next ~3 months, Sundays only)
uv run python check_campsites.py   # see --help for date range, day, dashboard, and tuning flags
```

## Testing & Linting

Each `campsite_checker/<module>.py` has a matching `tests/test_<module>.py`.
Keep that pairing when adding or splitting modules.

Before pushing, **always** run
`uv run ruff check . && uv run ruff format --check . && uv run pytest -v`,
and do not push while anything fails.

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
For ReserveAmerica, use the contract and facility components in a booking URL:
`/explore/<name>/<contract_code>/<campground_id>/...`.
