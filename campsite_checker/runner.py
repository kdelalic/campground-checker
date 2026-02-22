import gc
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from time import monotonic
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
from .results import count_matching_dates, group_results
from .search import execute_searches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress camply's chatty logs unless --verbose is passed
logging.getLogger("camply").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

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

    logger.info(
        "\U0001f3d5  %sChecking %d campgrounds for %s availability%s",
        prefix, len(entries), day_label, timestamp,
    )
    logger.info("   %s \u2192 %s (%d dates)", start_dt.date(), end_dt.date(), n_dates)


def run_once(
    entries: List[dict],
    args,
    day_filter: Optional[Set[int]],
    tg_token: Optional[str],
    tg_chat_id: Optional[str],
    scan_num: Optional[int] = None,
    dashboard_path: Optional[str] = None,
) -> Tuple[Set[Tuple[str, int, date]], List[Tuple[dict, List[AvailableCampsite]]]]:
    """Run one scan. Returns (current_keys, found_entries)."""
    scan_start = monotonic()
    start_dt, end_dt = compute_date_range(args)
    search_window = SearchWindow(start_date=start_dt, end_date=end_dt)

    print_scan_header(entries, start_dt, end_dt, day_filter, scan_num)

    results_by_index = execute_searches(entries, search_window, args)

    errors: List[str] = []
    found_entries: List[Tuple[dict, List[AvailableCampsite]]] = []
    current_keys: Set[Tuple[str, int, date]] = set()
    total_sites = 0

    for i, entry in enumerate(entries):
        entry, results, error = results_by_index[i]
        if error:
            errors.append(error)
            continue
        current_keys |= result_keys(entry, results, day_filter)
        grouped = group_results(results, day_filter)
        if grouped:
            _name, _by_date, count, _url = grouped
            total_sites += count
            found_entries.append((entry, results))

    for error in errors:
        logger.warning(error)

    elapsed = monotonic() - scan_start
    if found_entries:
        logger.info(
            "\U0001f3d5  Found %d site(s) at %d campground(s) (%.1fs)",
            total_sites, len(found_entries), elapsed,
        )
    else:
        logger.info("\U0001f3d5  No availability found. (%.1fs)", elapsed)

    if dashboard_path:
        from .dashboard import generate_dashboard

        generate_dashboard(found_entries, day_filter, dashboard_path)
        logger.info("   Dashboard written to %s", dashboard_path)

    return current_keys, found_entries


SENT_KEYS_FILE = Path(os.environ.get("SENT_KEYS_PATH", ".campsite_sent_keys.json"))


_SENT_KEYS_MAX_AGE_DAYS = 14  # Only keep keys for dates within this window


def _load_sent_keys(path: Path) -> Set[Tuple[str, int, date]]:
    """Load previously sent keys from disk, pruning stale entries."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        today = date.today()
        cutoff = today + timedelta(days=_SENT_KEYS_MAX_AGE_DAYS)
        keys = set()
        for name, cid, d in data:
            dt = date.fromisoformat(d)
            if today <= dt <= cutoff:
                keys.add((name, cid, dt))
        return keys
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


def _save_sent_keys(path: Path, keys: Set[Tuple[str, int, date]]) -> None:
    """Save sent keys to disk, pruning stale entries."""
    today = date.today()
    cutoff = today + timedelta(days=_SENT_KEYS_MAX_AGE_DAYS)
    data = sorted(
        [name, cid, d.isoformat()]
        for name, cid, d in keys
        if today <= d <= cutoff
    )
    path.write_text(json.dumps(data))


def _send_notifications(
    found_entries: List[Tuple[dict, List[AvailableCampsite]]],
    day_filter: Optional[Set[int]],
    tg_token: Optional[str],
    tg_chat_id: Optional[str],
    prev_keys: Optional[Set[Tuple[str, int, date]]] = None,
) -> int:
    """Filter and send Telegram notifications. Returns count of campgrounds notified.

    When *prev_keys* is provided (forever mode), only new results are sent.
    When *prev_keys* is None (single-run mode), all entries with alert=True are sent.
    """
    if not tg_token or not tg_chat_id or not found_entries:
        return 0

    if prev_keys is not None:
        alert_entries = [
            (entry, new_results)
            for entry, results in found_entries
            for new_results in [filter_new_results(entry, results, day_filter, prev_keys)]
            if new_results
        ]
    else:
        alert_entries = [
            (entry, results)
            for entry, results in found_entries
            if entry.get("alert", False)
        ]

    if not alert_entries:
        return 0

    msgs = build_telegram_message(alert_entries, day_filter)
    for msg in msgs:
        send_telegram(tg_token, tg_chat_id, msg)

    logger.info("\u2709 Telegram notification sent (%d campground(s))", len(alert_entries))
    return len(alert_entries)


def run_forever(
    entries: List[dict],
    raw_config: dict,
    config_path: str,
    args,
    day_filter: Optional[Set[int]],
    tg_token: Optional[str],
    tg_chat_id: Optional[str],
    dashboard_path: Optional[str] = None,
) -> None:
    from .bot import ConfigState, create_bot, start_bot_polling
    from .server import scan_status, start_healthcheck_server

    scan_status.interval_minutes = args.interval

    state = ConfigState(entries, raw_config, config_path, tg_chat_id or "")

    if tg_token and tg_chat_id:
        bot = create_bot(tg_token, state)
        start_bot_polling(bot)
        logger.info("Telegram bot commands active (/help for commands)")

    start_healthcheck_server()

    prev_keys = _load_sent_keys(SENT_KEYS_FILE)
    scan_num = 0

    r2_config = None
    if dashboard_path:
        from .upload import get_r2_config

        r2_config = get_r2_config(args, raw_config)

    try:
        while True:
            try:
                scan_num += 1

                with state.lock:
                    current_entries = list(state.entries)

                current_keys, found_entries = run_once(
                    current_entries, args, day_filter, tg_token, tg_chat_id, scan_num,
                    dashboard_path=dashboard_path,
                )

                if dashboard_path and r2_config:
                    from .upload import upload_to_r2

                    url = upload_to_r2(dashboard_path, r2_config)
                    if url:
                        logger.info("Dashboard uploaded to %s", url)

                _send_notifications(found_entries, day_filter, tg_token, tg_chat_id, prev_keys)

                prev_keys |= current_keys
                _save_sent_keys(SENT_KEYS_FILE, prev_keys)

                scan_status.update(entries_count=len(current_entries))

                # Free scan-local data before sleeping
                del current_keys, found_entries, current_entries

            except Exception as exc:
                logger.error("Scan #%d failed: %s", scan_num, exc, exc_info=True)
                scan_status.update(entries_count=0, error=True)

            # Force garbage collection between scans so Python returns memory
            # to the OS.  camply searchers, HTTP sessions, and pandas DataFrames
            # from the just-finished scan become unreachable here.
            gc.collect()

            logger.info("Next check in %d minute(s). Press Ctrl+C to stop.", args.interval)
            time.sleep(args.interval * 60)

    except KeyboardInterrupt:
        logger.info("\U0001f3d5  Stopped.")


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger("camply").setLevel(logging.INFO)

    entries, config = load_config(args.config)
    day_filter = resolve_day_filter(args)
    tg_token, tg_chat_id = get_telegram_creds(args, config)

    if (tg_token is None) != (tg_chat_id is None):
        sys.exit(
            "Error: both --telegram-token and --telegram-chat-id (or both env vars) are required"
        )

    from .dashboard import get_dashboard_path

    dashboard_path = get_dashboard_path(args, config)

    if args.forever:
        run_forever(
            entries, config, args.config, args, day_filter, tg_token, tg_chat_id,
            dashboard_path=dashboard_path,
        )
    else:
        current_keys, found_entries = run_once(
            entries, args, day_filter, tg_token, tg_chat_id,
            dashboard_path=dashboard_path,
        )
        _send_notifications(found_entries, day_filter, tg_token, tg_chat_id)

        if dashboard_path:
            from .upload import get_r2_config, upload_to_r2

            r2_config = get_r2_config(args, config)
            if r2_config:
                url = upload_to_r2(dashboard_path, r2_config)
                if url:
                    logger.info("Dashboard uploaded to %s", url)
