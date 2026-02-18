import argparse
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional, Set, Tuple

import yaml

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
        help="Run continuously, re-checking on every --interval minutes",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        metavar="MINUTES",
        help="Minutes between checks in --forever mode (default: 5)",
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
        sys.exit("Error: config must have a top-level 'campsites' list")

    entries = raw["campsites"]
    if not isinstance(entries, list) or len(entries) == 0:
        sys.exit("Error: 'campsites' must be a non-empty list")

    for i, entry in enumerate(entries):
        label = entry.get("name", f"entry #{i+1}")
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
        future = today + timedelta(days=91)
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
