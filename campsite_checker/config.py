import argparse
import sys
from datetime import date, datetime, timedelta

import yaml

from . import yaml_editor
from .providers import PROVIDER_MAP, WEEKDAY_NAMES


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check campsite availability using camply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--config",
        default="campsites.yaml",
        metavar="FILE",
        help="Path to YAML config file (default: campsites.yaml)",
    )
    parser.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="Search start date (default: today)",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Search end date (default: 3 months from today)",
    )
    parser.add_argument(
        "--nights",
        type=int,
        metavar="N",
        help="Override minimum consecutive nights for all entries",
    )
    parser.add_argument(
        "--weekends-only",
        action="store_true",
        default=False,
        help="Override: show only weekend availability for all entries",
    )
    parser.add_argument(
        "--day",
        nargs="+",
        metavar="WEEKDAY",
        help=(
            "Filter results to specific day(s) of the week (e.g. Saturday, Friday). Default: Sunday"
        ),
    )
    parser.add_argument(
        "--all-days",
        action="store_true",
        default=False,
        help="Show availability for all days of the week (overrides --day default)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show camply's own INFO-level log output",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        default=False,
        help="Run continuously, re-checking on every --alert-interval minutes",
    )
    parser.add_argument(
        "--alert-interval",
        "--interval",
        type=int,
        default=5,
        dest="alert_interval",
        metavar="MINUTES",
        help="Minutes between alert campground scans in --forever mode (default: 5)",
    )
    parser.add_argument(
        "--telegram-token",
        metavar="TOKEN",
        help="Telegram bot token (or set TELEGRAM_BOT_TOKEN env var, or telegram.bot_token in config)",
    )
    parser.add_argument(
        "--telegram-chat-id",
        metavar="ID",
        help="Telegram chat ID (or set TELEGRAM_CHAT_ID env var, or telegram.chat_id in config)",
    )
    parser.add_argument(
        "--dashboard",
        metavar="FILE",
        nargs="?",
        const="dashboard.html",
        default=None,
        help="Generate HTML dashboard (default path: dashboard.html)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        default=False,
        help="Disable dashboard generation even if configured in YAML",
    )
    parser.add_argument(
        "--r2-bucket",
        metavar="BUCKET",
        help="Cloudflare R2 bucket name for dashboard upload",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Max concurrent campsite search batches (default: 4)",
    )
    parser.add_argument(
        "--search-delay",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Minimum seconds between batch submissions to the same provider (default: 0)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        metavar="N",
        help="Maximum compatible campgrounds per Camply search batch (default: 4; use 1 to disable)",
    )
    parser.add_argument(
        "--dashboard-interval",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Minutes between dashboard-only campground scans in --forever mode (default: 60)",
    )
    return parser.parse_args()


def load_config(path: str) -> tuple[list[dict], dict]:
    """Returns (campsite entries, full raw config dict)."""
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        sys.exit(f"Error: config file not found: {path}")
    except yaml.YAMLError as exc:
        sys.exit(f"Error: invalid YAML in {path}: {exc}")

    if not isinstance(raw, dict) or "campsites" not in raw:
        sys.exit("Error: config must have a top-level 'campsites' key")

    campsites_raw = raw["campsites"]

    # Support two formats:
    #   1. Dict keyed by provider name → list of entries (new format)
    #   2. Flat list of entries, each with an optional 'provider' key (legacy)
    entries: list = []
    if isinstance(campsites_raw, dict):
        for provider, items in campsites_raw.items():
            if provider not in PROVIDER_MAP:
                sys.exit(
                    f"Error: unknown provider '{provider}'. "
                    f"Valid providers: {', '.join(PROVIDER_MAP)}"
                )
            if not isinstance(items, list) or len(items) == 0:
                sys.exit(f"Error: provider '{provider}' must contain a non-empty list")
            for entry in items:
                entry["provider"] = provider
                entries.append(entry)
    elif isinstance(campsites_raw, list):
        entries = campsites_raw
    else:
        sys.exit("Error: 'campsites' must be a dict of providers or a list of entries")

    # Filter out disabled entries (enabled: false)
    entries = [e for e in entries if e.get("enabled", True) is not False]

    if len(entries) == 0:
        sys.exit("Error: no campsite entries found")

    names = yaml_editor.parse_yaml_comments(path)
    for entry in entries:
        if "name" not in entry:
            cid = entry.get("campground_id")
            prov = entry.get("provider", "RecreationDotGov")
            if cid is not None:
                parsed_name = names.get((prov, int(cid)))
                if parsed_name:
                    entry["name"] = parsed_name

    # Apply defaults from top-level 'defaults' section
    defaults = raw.get("defaults") or {}
    default_nights = defaults.get("nights")
    if default_nights is not None and not isinstance(default_nights, int):
        sys.exit("Error: defaults.nights must be an integer")
    default_days = defaults.get("days")
    default_day_filter = None
    if default_days is not None:
        if not isinstance(default_days, list):
            sys.exit("Error: defaults.days must be a list of weekday names")
        default_day_filter = parse_day_names(default_days)

    for i, entry in enumerate(entries):
        label = f"entry #{i + 1}"
        if not entry.get("campground_id") and not entry.get("recreation_area"):
            sys.exit(f"Error in '{label}': must specify 'campground_id' or 'recreation_area'")
        provider = entry.get("provider", "RecreationDotGov")
        if provider not in PROVIDER_MAP:
            sys.exit(
                f"Error in '{label}': unknown provider '{provider}'. "
                f"Valid providers: {', '.join(PROVIDER_MAP)}"
            )
        alert = entry.get("alert")
        if alert is not None and not isinstance(alert, bool):
            sys.exit(f"Error in '{label}': alert must be true or false")

        criteria_raw = entry.get("criteria")
        days_raw = entry.get("days")

        if criteria_raw is not None and days_raw is not None:
            sys.exit(f"Error in '{label}': 'criteria' and 'days' are mutually exclusive")

        if criteria_raw is not None:
            if not isinstance(criteria_raw, list) or len(criteria_raw) == 0:
                sys.exit(f"Error in '{label}': 'criteria' must be a non-empty list")
            parsed_criteria = []
            for j, crit in enumerate(criteria_raw):
                if not isinstance(crit, dict):
                    sys.exit(f"Error in '{label}', criterion #{j + 1}: must be a mapping")
                crit_days = crit.get("days")
                crit_day_filter = None
                if crit_days is not None:
                    if not isinstance(crit_days, list):
                        sys.exit(
                            f"Error in '{label}', criterion #{j + 1}: "
                            "'days' must be a list of weekday names"
                        )
                    crit_day_filter = parse_day_names(crit_days)
                crit_nights = crit.get("nights")
                if crit_nights is not None and not isinstance(crit_nights, int):
                    sys.exit(f"Error in '{label}', criterion #{j + 1}: 'nights' must be an integer")
                parsed_criteria.append({"_day_filter": crit_day_filter, "nights": crit_nights})
            entry["_criteria"] = parsed_criteria
        else:
            entry["_criteria"] = None
            if days_raw is not None:
                if not isinstance(days_raw, list):
                    sys.exit(f"Error in '{label}': 'days' must be a list of weekday names")
                entry["_day_filter"] = parse_day_names(days_raw)
            else:
                entry["_day_filter"] = None

        # Apply default nights if not set per-entry
        if "nights" not in entry and default_nights is not None:
            entry["nights"] = default_nights

    # Store default day filter on raw config for resolve_day_filter
    raw["_default_day_filter"] = default_day_filter

    return entries, raw


def compute_date_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    today = date.today()

    if args.start:
        try:
            start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        except ValueError:
            sys.exit(f"Error: --start must be YYYY-MM-DD, got: {args.start}")
    else:
        start_dt = datetime(today.year, today.month, today.day)

    if args.end:
        try:
            end_dt = datetime.strptime(args.end, "%Y-%m-%d")
        except ValueError:
            sys.exit(f"Error: --end must be YYYY-MM-DD, got: {args.end}")
    else:
        future = today + timedelta(days=181)
        end_dt = datetime(future.year, future.month, future.day)

    if end_dt <= start_dt:
        sys.exit("Error: --end must be after --start")

    return start_dt, end_dt


def parse_day_names(names: list) -> set[int] | None:
    """Convert a list of weekday name strings to a set of weekday ints.

    Returns ``None`` if *names* is empty (meaning "use global default").
    """
    if not names:
        return None
    days: set[int] = set()
    for name in names:
        key = str(name).lower()
        if key not in WEEKDAY_NAMES:
            valid = ", ".join(d.capitalize() for d in WEEKDAY_NAMES)
            sys.exit(f"Error: unknown weekday '{name}'. Valid: {valid}")
        days.add(WEEKDAY_NAMES[key])
    return days if days else None


def resolve_day_filter(args: argparse.Namespace, config: dict | None = None) -> set[int] | None:
    """Return a set of weekday integers to filter on, or None for no filter.

    Priority: --all-days > --day > defaults.days (YAML) > Sunday.
    """
    if args.all_days:
        return None
    if args.day:
        return parse_day_names(args.day)
    if config is not None:
        default_filter = config.get("_default_day_filter")
        if default_filter is not None:
            return default_filter
    # Default: Sundays only
    return {6}


def resolve_entry_day_filter(entry: dict, global_day_filter: set[int] | None) -> set[int] | None:
    """Return the effective day filter for an entry.

    Uses the entry's per-entry ``_day_filter`` if set, otherwise falls back
    to *global_day_filter*.
    """
    entry_filter = entry.get("_day_filter")
    if entry_filter is not None:
        return entry_filter
    return global_day_filter


def expand_search_tasks(
    entries: list[dict], global_day_filter: set[int] | None
) -> list[tuple[int, dict, set[int] | None]]:
    """Expand entries with criteria into individual search tasks.

    Returns a list of ``(original_entry_index, entry_for_search,
    effective_day_filter)`` tuples.  Entries with ``_criteria`` produce one
    task per criterion (shallow-copied entry with the criterion's ``nights``
    override).  Entries without criteria produce a single task.
    """
    tasks: list[tuple[int, dict, set[int] | None]] = []
    for i, entry in enumerate(entries):
        criteria = entry.get("_criteria")
        if criteria:
            for crit in criteria:
                task_entry = dict(entry)
                if crit.get("nights") is not None:
                    task_entry["nights"] = crit["nights"]
                crit_filter = crit["_day_filter"]
                effective = crit_filter if crit_filter is not None else global_day_filter
                tasks.append((i, task_entry, effective))
        else:
            effective = resolve_entry_day_filter(entry, global_day_filter)
            tasks.append((i, entry, effective))
    return tasks
