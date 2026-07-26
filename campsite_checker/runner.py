import gc
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic

from camply.containers import SearchWindow

from .config import (
    compute_date_range,
    expand_search_tasks,
    load_config,
    parse_args,
    resolve_day_filter,
)
from .notify import (
    build_processed_telegram_message,
    filter_new_availability,
    get_telegram_creds,
    send_telegram,
)
from .providers import WEEKDAY_NAMES
from .results import (
    NotificationKey,
    ProcessedAvailability,
    count_matching_dates,
    filter_results,
    format_processed_results,
    process_filtered_results,
)
from .search import execute_searches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress camply's chatty logs unless --verbose is passed
logging.getLogger("camply").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _resolve_dashboard_interval(args, raw_config: dict) -> int:
    """Resolve dashboard scan interval in minutes.

    Priority: --dashboard-interval CLI > dashboard.interval YAML > default 60.
    """
    cli_val = getattr(args, "dashboard_interval", None)
    if cli_val is not None:
        return cli_val
    dash_cfg = raw_config.get("dashboard") or {}
    yaml_val = dash_cfg.get("interval")
    if yaml_val is not None:
        return int(yaml_val)
    return 60


def print_scan_header(
    entries: list[dict],
    start_dt: datetime,
    end_dt: datetime,
    day_filter: set[int] | None,
    scan_num: int | None = None,
    scan_type: str | None = None,
) -> None:
    inv = {v: k for k, v in WEEKDAY_NAMES.items()}
    if day_filter is None:
        day_label = "all-day"
    elif len(day_filter) == 1:
        day_label = inv[next(iter(day_filter))].capitalize()
    else:
        day_label = "/".join(inv[d].capitalize() for d in sorted(day_filter))

    n_dates = count_matching_dates(start_dt, end_dt, day_filter)

    parts = []
    if scan_num is not None:
        parts.append(f"scan #{scan_num}")
    if scan_type is not None:
        parts.append(scan_type)
    prefix = f"[{' / '.join(parts)}] " if parts else ""
    timestamp = f" — {datetime.now().strftime('%H:%M:%S')}" if scan_num is not None else ""

    logger.info(
        "\U0001f3d5  %sChecking %d campgrounds for %s availability%s",
        prefix,
        len(entries),
        day_label,
        timestamp,
    )
    logger.info("   %s \u2192 %s (%d dates)", start_dt.date(), end_dt.date(), n_dates)


def run_once(
    entries: list[dict],
    args,
    day_filter: set[int] | None,
    tg_token: str | None,
    tg_chat_id: str | None,
    scan_num: int | None = None,
    dashboard_path: str | None = None,
    scan_type: str | None = None,
) -> tuple[
    set[NotificationKey],
    list[ProcessedAvailability],
    list[ProcessedAvailability],
]:
    """Run one scan and return keys, available entries, and every processed entry."""
    scan_start = monotonic()
    start_dt, end_dt = compute_date_range(args)
    search_window = SearchWindow(start_date=start_dt, end_date=end_dt)

    print_scan_header(entries, start_dt, end_dt, day_filter, scan_num, scan_type=scan_type)

    # Expand entries with criteria into individual search tasks
    search_tasks = expand_search_tasks(entries, day_filter)
    task_entries = [entry for _, entry, _ in search_tasks]

    results_by_task = execute_searches(task_entries, search_window, args)

    # Collect, filter per-task, and merge results back per original entry
    errors: set[str] = set()
    entry_filtered: dict = defaultdict(list)
    for task_idx, (orig_idx, _task_entry, task_filter) in enumerate(search_tasks):
        _te, results, error = results_by_task[task_idx]
        if error:
            errors.add(error)
            continue
        filtered = filter_results(results, task_filter)
        entry_filtered[orig_idx].extend(filtered)

    # Deduplicate per original entry and build output
    found_entries: list[ProcessedAvailability] = []
    all_with_results: list[ProcessedAvailability] = []
    current_keys: set[NotificationKey] = set()
    total_sites = 0

    for i, entry in enumerate(entries):
        availability = process_filtered_results(entry, entry_filtered.get(i, []))
        all_with_results.append(availability)
        current_keys.update(availability.notification_keys)
        if availability.available:
            total_sites += availability.total_sites
            found_entries.append(availability)

    for error in errors:
        logger.warning(error)

    for availability in found_entries:
        formatted = format_processed_results(availability)
        if formatted:
            print(formatted)

    elapsed = monotonic() - scan_start
    if found_entries:
        logger.info(
            "\U0001f3d5  Found %d site(s) at %d campground(s) (%.1fs)",
            total_sites,
            len(found_entries),
            elapsed,
        )
    else:
        logger.info("\U0001f3d5  No availability found. (%.1fs)", elapsed)

    if dashboard_path:
        from .dashboard import generate_dashboard

        generate_dashboard(all_with_results, None, dashboard_path)
        logger.info("   Dashboard written to %s", dashboard_path)

    return current_keys, found_entries, all_with_results


SENT_KEYS_FILE = Path(os.environ.get("SENT_KEYS_PATH", ".campsite_sent_keys.json"))


_SENT_KEYS_MAX_AGE_DAYS = 14  # Only keep keys for dates within this window


def _load_sent_keys(path: Path) -> set[NotificationKey]:
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


def _save_sent_keys(path: Path, keys: set[NotificationKey]) -> None:
    """Save sent keys to disk, pruning stale entries."""
    today = date.today()
    cutoff = today + timedelta(days=_SENT_KEYS_MAX_AGE_DAYS)
    data = [[name, cid, d.isoformat()] for name, cid, d in keys if today <= d <= cutoff]
    data.sort(key=lambda row: (row[0], str(row[1]), row[2]))
    path.write_text(json.dumps(data))


def _send_notifications(
    found_entries: list[ProcessedAvailability],
    tg_token: str | None,
    tg_chat_id: str | None,
    prev_keys: set[NotificationKey] | None = None,
) -> int:
    """Filter and send Telegram notifications. Returns count of campgrounds notified.

    When *prev_keys* is provided (forever mode), only new results are sent.
    When *prev_keys* is None (single-run mode), all entries with alert=True are sent.
    """
    if not tg_token or not tg_chat_id or not found_entries:
        return 0

    if prev_keys is not None:
        alert_entries = [
            new_availability
            for availability in found_entries
            for new_availability in [filter_new_availability(availability, prev_keys)]
            if new_availability.available
        ]
    else:
        alert_entries = [
            availability for availability in found_entries if availability.entry.get("alert", False)
        ]

    if not alert_entries:
        return 0

    msgs = build_processed_telegram_message(alert_entries)
    for msg in msgs:
        send_telegram(tg_token, tg_chat_id, msg)

    logger.info("\u2709 Telegram notification sent (%d campground(s))", len(alert_entries))
    return len(alert_entries)


def run_forever(
    entries: list[dict],
    raw_config: dict,
    config_path: str,
    args,
    day_filter: set[int] | None,
    tg_token: str | None,
    tg_chat_id: str | None,
    dashboard_path: str | None = None,
) -> None:
    from .bot import ConfigState, create_bot, start_bot_polling
    from .server import scan_status, start_healthcheck_server

    scan_status.alert_interval_minutes = args.alert_interval

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

    dashboard_interval = _resolve_dashboard_interval(args, raw_config)
    last_dashboard_scan: float = 0.0  # monotonic; 0 forces first-iteration scan
    cached_dashboard_results: list[ProcessedAvailability] = []

    scan_status.dashboard_interval_minutes = dashboard_interval

    logger.info(
        "Alert interval: %d min, dashboard interval: %d min",
        args.alert_interval,
        dashboard_interval,
    )

    try:
        while True:
            try:
                scan_num += 1

                with state.lock:
                    current_entries = list(state.entries)

                alert_entries = [e for e in current_entries if e.get("alert", False)]
                dashboard_entries = [e for e in current_entries if not e.get("alert", False)]

                # --- Alert scan (every iteration) ---
                alert_keys: set[NotificationKey] = set()
                alert_found: list[ProcessedAvailability] = []
                if alert_entries:
                    alert_keys, alert_found, alert_all = run_once(
                        alert_entries,
                        args,
                        day_filter,
                        tg_token,
                        tg_chat_id,
                        scan_num,
                        dashboard_path=None,
                        scan_type="alert",
                    )
                else:
                    alert_keys, alert_found, alert_all = set(), [], []

                # --- Dashboard-only scan (when interval elapsed) ---
                now = monotonic()
                dashboard_due = (now - last_dashboard_scan) >= dashboard_interval * 60
                dash_keys: set[NotificationKey] = set()

                if dashboard_due and dashboard_entries:
                    dash_keys, dash_found, dash_all = run_once(
                        dashboard_entries,
                        args,
                        day_filter,
                        tg_token,
                        tg_chat_id,
                        scan_num,
                        dashboard_path=None,
                        scan_type="dashboard",
                    )
                    cached_dashboard_results = dash_all
                    last_dashboard_scan = monotonic()
                    scan_status.last_dashboard_scan = datetime.now()
                elif dashboard_due:
                    cached_dashboard_results = []
                    last_dashboard_scan = monotonic()
                    scan_status.last_dashboard_scan = datetime.now()

                # --- Generate dashboard from merged results ---
                if dashboard_path:
                    from .dashboard import generate_dashboard

                    merged = alert_all + cached_dashboard_results
                    generate_dashboard(merged, None, dashboard_path)
                    logger.info("   Dashboard written to %s", dashboard_path)

                    if r2_config:
                        from .upload import upload_to_r2

                        url = upload_to_r2(dashboard_path, r2_config)
                        if url:
                            logger.info("Dashboard uploaded to %s", url)

                # --- Notifications (alert entries only) ---
                _send_notifications(alert_found, tg_token, tg_chat_id, prev_keys)

                # --- Persist dedup keys from both tiers ---
                current_keys = alert_keys | dash_keys
                prev_keys |= current_keys
                _save_sent_keys(SENT_KEYS_FILE, prev_keys)

                scan_status.update(entries_count=len(current_entries))

                # Free scan-local data before sleeping
                del alert_keys, alert_found, alert_all, dash_keys, current_keys, current_entries

            except Exception as exc:
                logger.error("Scan #%d failed: %s", scan_num, exc, exc_info=True)
                scan_status.update(entries_count=0, error=True)

            # Force garbage collection between scans so Python returns memory
            # to the OS.  camply searchers, HTTP sessions, and pandas DataFrames
            # from the just-finished scan become unreachable here.
            gc.collect()

            mins_since_dash = (monotonic() - last_dashboard_scan) / 60
            mins_until_dash = max(0, dashboard_interval - mins_since_dash)
            logger.info(
                "Next alert check in %d min. Next dashboard scan in ~%d min. Ctrl+C to stop.",
                args.alert_interval,
                int(mins_until_dash),
            )
            time.sleep(args.alert_interval * 60)

    except KeyboardInterrupt:
        logger.info("\U0001f3d5  Stopped.")


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger("camply").setLevel(logging.INFO)

    entries, config = load_config(args.config)
    day_filter = resolve_day_filter(args, config)
    tg_token, tg_chat_id = get_telegram_creds(args, config)

    if (tg_token is None) != (tg_chat_id is None):
        sys.exit(
            "Error: both --telegram-token and --telegram-chat-id (or both env vars) are required"
        )

    from .dashboard import get_dashboard_path

    dashboard_path = get_dashboard_path(args, config)

    if args.forever:
        run_forever(
            entries,
            config,
            args.config,
            args,
            day_filter,
            tg_token,
            tg_chat_id,
            dashboard_path=dashboard_path,
        )
    else:
        current_keys, found_entries, _ = run_once(
            entries,
            args,
            day_filter,
            tg_token,
            tg_chat_id,
            dashboard_path=dashboard_path,
        )
        _send_notifications(found_entries, tg_token, tg_chat_id)

        if dashboard_path:
            from .upload import get_r2_config, upload_to_r2

            r2_config = get_r2_config(args, config)
            if r2_config:
                url = upload_to_r2(dashboard_path, r2_config)
                if url:
                    logger.info("Dashboard uploaded to %s", url)
