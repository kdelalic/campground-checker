import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

from camply.containers import AvailableCampsite, SearchWindow

from .config import compute_date_range, load_config, parse_args, resolve_day_filter
from .notify import (
    build_telegram_message,
    filter_new_results,
    get_telegram_creds,
    result_keys,
    send_telegram,
)
from .providers import WEEKDAY_NAMES
from .results import count_matching_dates, format_results
from .search import execute_searches

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
)

def print_scan_header(
    entries: List[dict],
    start_dt: datetime,
    end_dt: datetime,
    day_filter: Optional[Set[int]],
    scan_num: Optional[int] = None,
) -> None:
    inv = {v: k for k, v in WEEKDAY_NAMES.items()}
    if day_filter is None:
        day_label = "all-day"
    elif len(day_filter) == 1:
        day_label = inv[next(iter(day_filter))].capitalize()
    else:
        day_label = "/".join(inv[d].capitalize() for d in sorted(day_filter))

    n_dates = count_matching_dates(start_dt, end_dt, day_filter)

    prefix = f"[scan #{scan_num}] " if scan_num is not None else ""
    timestamp = f" — {datetime.now().strftime('%H:%M:%S')}" if scan_num is not None else ""

    print(
        f"\U0001f3d5  {prefix}Checking {len(entries)} campgrounds "
        f"for {day_label} availability{timestamp}"
    )
    print(f"   {start_dt.date()} \u2192 {end_dt.date()} ({n_dates} dates)")
    sys.stdout.flush()


def run_once(
    entries: List[dict],
    args,
    day_filter: Optional[Set[int]],
    tg_token: Optional[str],
    tg_chat_id: Optional[str],
    scan_num: Optional[int] = None,
) -> Tuple[Set[Tuple[str, int, date]], List[Tuple[dict, List[AvailableCampsite]]]]:
    """Run one scan. Returns (current_keys, found_entries)."""
    start_dt, end_dt = compute_date_range(args)
    search_window = SearchWindow(start_date=start_dt, end_date=end_dt)

    print_scan_header(entries, start_dt, end_dt, day_filter, scan_num)

    results_by_index = execute_searches(entries, search_window, args)

    errors: List[str] = []
    outputs: List[str] = []
    found_entries: List[Tuple[dict, List[AvailableCampsite]]] = []
    current_keys: Set[Tuple[str, int, date]] = set()

    for i, entry in enumerate(entries):
        entry, results, error = results_by_index[i]
        if error:
            errors.append(error)
            continue
        current_keys |= result_keys(entry, results, day_filter)
        output = format_results(entry, results, day_filter)
        if output:
            outputs.append(output)
            found_entries.append((entry, results))

    if outputs:
        print("\U0001f3d5  Campsite Availability Found!")
        for output in outputs:
            print(output)
    else:
        print("\U0001f3d5  No availability found.")

    for error in errors:
        print(error, file=sys.stderr)

    return current_keys, found_entries


SENT_KEYS_FILE = Path(".campsite_sent_keys.json")


def _load_sent_keys(path: Path) -> Set[Tuple[str, int, date]]:
    """Load previously sent keys from disk, pruning dates before today."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        today = date.today()
        keys = set()
        for name, cid, d in data:
            dt = date.fromisoformat(d)
            if dt >= today:
                keys.add((name, cid, dt))
        return keys
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


def _save_sent_keys(path: Path, keys: Set[Tuple[str, int, date]]) -> None:
    """Save sent keys to disk, pruning dates before today."""
    today = date.today()
    data = sorted(
        [name, cid, d.isoformat()]
        for name, cid, d in keys
        if d >= today
    )
    path.write_text(json.dumps(data))


def run_forever(
    entries: List[dict],
    raw_config: dict,
    config_path: str,
    args,
    day_filter: Optional[Set[int]],
    tg_token: Optional[str],
    tg_chat_id: Optional[str],
) -> None:
    from .bot import ConfigState, create_bot, start_bot_polling

    state = ConfigState(entries, raw_config, config_path, tg_chat_id or "")

    if tg_token and tg_chat_id:
        bot = create_bot(tg_token, state)
        start_bot_polling(bot)
        print("   Telegram bot commands active (/help for commands)")

    prev_keys = _load_sent_keys(SENT_KEYS_FILE)
    scan_num = 0
    try:
        while True:
            scan_num += 1

            with state.lock:
                current_entries = list(state.entries)

            current_keys, found_entries = run_once(
                current_entries, args, day_filter, tg_token, tg_chat_id, scan_num
            )

            new_entries = [
                (entry, new_results)
                for entry, results in found_entries
                for new_results in [filter_new_results(entry, results, day_filter, prev_keys)]
                if new_results
            ]

            if new_entries and tg_token and tg_chat_id:
                msg = build_telegram_message(new_entries, day_filter)
                send_telegram(tg_token, tg_chat_id, msg)
                print(f"   \u2709 Telegram notification sent ({len(new_entries)} campground(s))")

            prev_keys |= current_keys
            _save_sent_keys(SENT_KEYS_FILE, prev_keys)

            print(f"\n   Next check in {args.interval} minute(s). Press Ctrl+C to stop.\n")
            sys.stdout.flush()
            time.sleep(args.interval * 60)

    except KeyboardInterrupt:
        print("\n\U0001f3d5  Stopped.")


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    entries, config = load_config(args.config)
    day_filter = resolve_day_filter(args)
    tg_token, tg_chat_id = get_telegram_creds(args, config)

    if (tg_token is None) != (tg_chat_id is None):
        sys.exit(
            "Error: both --telegram-token and --telegram-chat-id (or both env vars) are required"
        )

    if args.forever:
        run_forever(entries, config, args.config, args, day_filter, tg_token, tg_chat_id)
    else:
        current_keys, found_entries = run_once(
            entries, args, day_filter, tg_token, tg_chat_id
        )
        if found_entries and tg_token and tg_chat_id:
            msg = build_telegram_message(found_entries, day_filter)
            send_telegram(tg_token, tg_chat_id, msg)
            print(f"   \u2709 Telegram notification sent ({len(found_entries)} campground(s))")
