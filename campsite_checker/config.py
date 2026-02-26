import argparse
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional, Set, Tuple

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
            "Filter results to specific day(s) of the week "
            "(e.g. Saturday, Friday). Default: Saturday"
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
        default=2,
        metavar="N",
        help="Max concurrent campsite searches (default: 2; reduce for low-CPU environments)",
    )
    parser.add_argument(
        "--search-delay",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Seconds to sleep between search submissions (default: 0; use 1-2 on low-CPU environments)",
    )
    parser.add_argument(
        "--dashboard-interval",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Minutes between dashboard-only campground scans in --forever mode (default: 60)",
    )
    return parser.parse_args()


def load_config(path: str) -> Tuple[List[dict], dict]:
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

    for i, entry in enumerate(entries):
        label = f"entry #{i+1}"
        if not entry.get("campground_id") and not entry.get("recreation_area"):
            sys.exit(
                f"Error in '{label}': must specify 'campground_id' or 'recreation_area'"
            )
        provider = entry.get("provider", "RecreationDotGov")
        if provider not in PROVIDER_MAP:
            sys.exit(
                f"Error in '{label}': unknown provider '{provider}'. "
                f"Valid providers: {', '.join(PROVIDER_MAP)}"
            )
        alert = entry.get("alert")
        if alert is not None and not isinstance(alert, bool):
            sys.exit(f"Error in '{label}': alert must be true or false")

    return entries, raw


def compute_date_range(args: argparse.Namespace) -> Tuple[datetime, datetime]:
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


def resolve_day_filter(args: argparse.Namespace) -> Optional[Set[int]]:
    """Return a set of weekday integers to filter on, or None for no filter."""
    if args.all_days:
        return None

    if args.day:
        days: Set[int] = set()
        for name in args.day:
            key = name.lower()
            if key not in WEEKDAY_NAMES:
                valid = ", ".join(d.capitalize() for d in WEEKDAY_NAMES)
                sys.exit(f"Error: unknown weekday '{name}'. Valid: {valid}")
            days.add(WEEKDAY_NAMES[key])
        return days

    # Default: Saturdays only
    return {5}
