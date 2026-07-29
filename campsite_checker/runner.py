import concurrent.futures
import gc
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from camply.containers import SearchWindow

from .config import (
    DateWindowExhausted,
    compute_date_range,
    expand_search_tasks,
    load_config,
    parse_args,
    resolve_day_filter,
)
from .dispatch import PRIORITY_ALERT, PRIORITY_DASHBOARD, shutdown_dispatcher
from .notify import (
    build_processed_telegram_message,
    filter_new_availability,
    get_telegram_creds,
    send_telegram,
)
from .results import (
    NotificationKey,
    ProcessedAvailability,
    count_matching_dates,
    filter_results,
    format_processed_results,
    process_filtered_results,
)
from .search import effective_nights, execute_searches
from .state import SENT_KEYS_FILE, load_sent_keys, save_sent_keys
from .weekdays import WEEKDAY_LABELS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress camply's chatty logs unless --verbose is passed
logging.getLogger("camply").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Minimum idle time between the end of one dashboard sweep and the start of the
# next, regardless of how far the sweep overran --dashboard-interval.
MIN_DASHBOARD_IDLE_SECONDS = 60.0


def _resident_memory_mb() -> float | None:
    """Return current RSS on Linux or peak RSS on other Unix platforms."""
    try:
        statm = Path("/proc/self/statm").read_text().split()
        return int(statm[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        pass

    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return rss / divisor
    except (ImportError, OSError, ValueError):
        return None


def _maybe_collect_garbage(scan_num: int, interval: int) -> bool:
    """Run and measure a full collection at the configured scan interval."""
    if interval <= 0 or scan_num % interval != 0:
        return False
    before_mb = _resident_memory_mb()
    generation_counts = gc.get_count()
    started = monotonic()
    collected = gc.collect()
    elapsed = monotonic() - started
    after_mb = _resident_memory_mb()
    memory_label = (
        f", memory {before_mb:.1f}→{after_mb:.1f} MiB"
        if before_mb is not None and after_mb is not None
        else ""
    )
    logger.info(
        "Garbage collection: %d object(s), %.3fs%s (generations before: %s)",
        collected,
        elapsed,
        memory_label,
        generation_counts,
    )
    return True


def _advance_poll_deadline(
    previous_deadline: float,
    period_seconds: float,
    now: float,
) -> float:
    """Advance an anchored poll deadline, skipping periods lost to overruns."""
    next_deadline = previous_deadline + period_seconds
    if next_deadline <= now:
        missed_periods = int((now - next_deadline) // period_seconds) + 1
        next_deadline += missed_periods * period_seconds
    return next_deadline


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
    if day_filter is None:
        day_label = "all-day"
    elif len(day_filter) == 1:
        day_label = WEEKDAY_LABELS[next(iter(day_filter))]
    else:
        day_label = "/".join(WEEKDAY_LABELS[d] for d in sorted(day_filter))

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

    # Record the stay lengths actually searched so the dashboard can label each
    # card. Neither the `--nights` override nor a `criteria` block is visible
    # from an entry's own `nights` key, and one entry can search several.
    nights_by_entry: dict[int, set[int]] = defaultdict(set)
    for orig_idx, task_entry, _task_filter in search_tasks:
        nights_by_entry[orig_idx].add(effective_nights(task_entry, args))
    for orig_idx, nights in nights_by_entry.items():
        entries[orig_idx]["_searched_nights"] = sorted(nights)

    priority = PRIORITY_DASHBOARD if scan_type == "dashboard" else PRIORITY_ALERT
    results_by_task = execute_searches(task_entries, search_window, args, priority=priority)

    # Collect, filter per-task, and merge results back per original entry
    errors: set[str] = set()
    failed_entries: set[int] = set()
    entry_filtered: dict = defaultdict(list)
    for task_idx, (orig_idx, _task_entry, task_filter) in enumerate(search_tasks):
        _te, results, error = results_by_task[task_idx]
        if error:
            errors.add(error)
            failed_entries.add(orig_idx)
            continue
        filtered = filter_results(results, task_filter)
        entry_filtered[orig_idx].extend(filtered)

    # Deduplicate per original entry and build output
    found_entries: list[ProcessedAvailability] = []
    all_with_results: list[ProcessedAvailability] = []
    current_keys: set[NotificationKey] = set()
    total_sites = 0

    for i, entry in enumerate(entries):
        availability = process_filtered_results(
            entry,
            entry_filtered.get(i, []),
            search_succeeded=i not in failed_entries,
        )
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
        from .dashboard import build_search_filter_view, generate_dashboard

        # `all_with_results` is already filtered, hence the `None` day filter;
        # the view describes what that filtering was.
        generate_dashboard(
            all_with_results,
            None,
            dashboard_path,
            build_search_filter_view(day_filter, start_dt, end_dt),
        )
        logger.info("   Dashboard written to %s", dashboard_path)

    return current_keys, found_entries, all_with_results


def _send_notifications(
    found_entries: list[ProcessedAvailability],
    tg_token: str | None,
    tg_chat_id: str | None,
    prev_keys: set[NotificationKey] | None = None,
) -> tuple[int, int]:
    """Filter and send Telegram notifications. Returns (sent, failed) message counts.

    When *prev_keys* is provided (forever mode), only new results are sent.
    When *prev_keys* is None (single-run mode), all entries with alert=True are sent.
    """
    if not tg_token or not tg_chat_id or not found_entries:
        return 0, 0

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
        return 0, 0

    msgs = build_processed_telegram_message(alert_entries)
    sent = 0
    failed = 0
    for msg in msgs:
        if send_telegram(tg_token, tg_chat_id, msg):
            sent += 1
        else:
            failed += 1

    if failed:
        logger.warning(
            "\u2709 %d of %d Telegram message(s) failed to send (%d campground(s))",
            failed,
            sent + failed,
            len(alert_entries),
        )
    else:
        logger.info("\u2709 Telegram notification sent (%d campground(s))", len(alert_entries))
    return sent, failed


@dataclass(slots=True)
class DashboardScanOutcome:
    results: list[ProcessedAvailability]
    duration_seconds: float
    completed_at: datetime
    error: Exception | None = None


def _run_dashboard_scan(
    entries: list[dict],
    args,
    day_filter: set[int] | None,
    tg_token: str | None,
    tg_chat_id: str | None,
    scan_num: int,
) -> DashboardScanOutcome:
    """Run dashboard-only work in the background and retain its completion time.

    Dashboard-only entries never alert, so their notification keys are
    deliberately not merged into the sent-key state: pre-marking them as sent
    would suppress the first alerts if an entry is later switched to
    ``alert: true``.
    """
    started = monotonic()
    try:
        _keys, _found, all_results = run_once(
            entries,
            args,
            day_filter,
            tg_token,
            tg_chat_id,
            scan_num,
            dashboard_path=None,
            scan_type="dashboard",
        )
        error = None
    except Exception as exc:
        all_results, error = [], exc
    return DashboardScanOutcome(
        results=all_results,
        duration_seconds=monotonic() - started,
        completed_at=datetime.now(timezone.utc),
        error=error,
    )


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
    from .metrics import CampgroundMetric
    from .server import start_healthcheck_server
    from .status import scan_status

    scan_status.alert_interval_minutes = args.alert_interval

    state = ConfigState(entries, raw_config, config_path, tg_chat_id or "")

    if tg_token and tg_chat_id:
        # Never let Telegram unavailability at boot stop scanning: the bot is
        # a convenience, alert scans are the point.
        try:
            bot = create_bot(tg_token, state)
            scan_status.bot_thread = start_bot_polling(bot)
            logger.info("Telegram bot commands active (/help for commands)")
        except Exception as exc:
            logger.warning("Telegram bot setup failed; continuing without bot commands: %s", exc)

    start_healthcheck_server()

    prev_keys = load_sent_keys(SENT_KEYS_FILE)
    scan_num = 0

    dashboard_publisher = None
    if dashboard_path:
        from .dashboard import DashboardPublisher, build_search_filter_view
        from .upload import R2Uploader, get_r2_config

        r2_config = get_r2_config(args, raw_config)
        r2_uploader = None
        if r2_config:
            r2_uploader = R2Uploader(r2_config)
        dashboard_publisher = DashboardPublisher(dashboard_path, r2_uploader)

    dashboard_interval = _resolve_dashboard_interval(args, raw_config)
    # Schedule the first dashboard scan immediately, regardless of host uptime.
    # A zero sentinel does not work on freshly booted hosts whose monotonic clock
    # is still below the configured interval.
    last_dashboard_started = monotonic() - dashboard_interval * 60
    last_dashboard_finished = last_dashboard_started
    cached_dashboard_results: list[ProcessedAvailability] = []
    dashboard_snapshot_ready = False
    dashboard_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="dashboard-scan",
    )
    dashboard_future: concurrent.futures.Future[DashboardScanOutcome] | None = None
    gc_interval = max(0, int(getattr(args, "gc_interval", 12)))
    alert_period_seconds = max(1.0, args.alert_interval * 60)
    next_alert_deadline = monotonic()

    scan_status.dashboard_interval_minutes = dashboard_interval

    logger.info(
        "Alert interval: %d min, dashboard interval: %d min",
        args.alert_interval,
        dashboard_interval,
    )

    try:
        while True:
            scan_started = monotonic()
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

                scan_status.mark_alert_scan()

                # Notify and checkpoint alert keys before lower-priority dashboard
                # work. The checkpoint is skipped when any message failed so an
                # undelivered alert is retried next scan instead of being lost.
                sent_msgs, failed_msgs = _send_notifications(
                    alert_found, tg_token, tg_chat_id, prev_keys
                )
                scan_status.record_notifications(sent=sent_msgs, failed=failed_msgs)
                if failed_msgs == 0:
                    prev_keys |= alert_keys
                    if save_sent_keys(SENT_KEYS_FILE, prev_keys):
                        logger.debug("Updated sent-key state at %s", SENT_KEYS_FILE)
                else:
                    logger.warning(
                        "Deferring sent-key checkpoint: %d Telegram message(s) "
                        "failed; new availability will be retried next scan",
                        failed_msgs,
                    )

                # Collect completed dashboard work after the priority alert path.
                if dashboard_future is not None and dashboard_future.done():
                    try:
                        dashboard_outcome = dashboard_future.result()
                    except Exception as exc:
                        logger.error("Dashboard scan failed: %s", exc, exc_info=True)
                        scan_status.finish_dashboard_scan(
                            duration_seconds=max(0.0, monotonic() - last_dashboard_started),
                            error=True,
                        )
                    else:
                        if dashboard_outcome.error is not None:
                            logger.error("Dashboard scan failed: %s", dashboard_outcome.error)
                            scan_status.finish_dashboard_scan(
                                duration_seconds=dashboard_outcome.duration_seconds,
                                error=True,
                                when=dashboard_outcome.completed_at,
                            )
                        else:
                            dashboard_snapshot_ready = True
                            cached_dashboard_results = dashboard_outcome.results
                            scan_status.finish_dashboard_scan(
                                duration_seconds=dashboard_outcome.duration_seconds,
                                when=dashboard_outcome.completed_at,
                            )
                    dashboard_future = None
                    last_dashboard_finished = monotonic()

                # Start due dashboard-only work without blocking the alert scheduler.
                now = monotonic()
                dashboard_due = (
                    dashboard_future is None
                    and (now - last_dashboard_started) >= dashboard_interval * 60
                    # A sweep that overruns its own interval is due the instant
                    # it finishes, which would run sweeps back to back and raise
                    # the request rate exactly when the provider is already
                    # throttling. Always leave the provider an idle gap.
                    and (now - last_dashboard_finished) >= MIN_DASHBOARD_IDLE_SECONDS
                )

                if dashboard_due and dashboard_entries:
                    last_dashboard_started = now
                    dashboard_future = dashboard_executor.submit(
                        _run_dashboard_scan,
                        dashboard_entries,
                        args,
                        day_filter,
                        tg_token,
                        tg_chat_id,
                        scan_num,
                    )
                    scan_status.start_dashboard_scan()
                    logger.info(
                        "Started background dashboard scan for %d campground(s)",
                        len(dashboard_entries),
                    )
                elif dashboard_due:
                    cached_dashboard_results = []
                    dashboard_snapshot_ready = True
                    last_dashboard_started = now
                    scan_status.start_dashboard_scan()
                    scan_status.finish_dashboard_scan(duration_seconds=0)

                # --- Generate dashboard from merged results ---
                if dashboard_publisher is not None and dashboard_snapshot_ready:
                    merged = alert_all + cached_dashboard_results
                    # Recomputed per publish: a relative window rolls forward,
                    # so the rendered range changes without any config change.
                    window_start, window_end = compute_date_range(args)
                    publish_result = dashboard_publisher.publish(
                        merged,
                        search_filter=build_search_filter_view(
                            day_filter, window_start, window_end
                        ),
                    )
                    if publish_result.written:
                        logger.info("   Dashboard written to %s", dashboard_path)
                    else:
                        logger.debug("Dashboard availability unchanged; skipping rewrite")
                    if publish_result.uploaded:
                        if publish_result.public_url:
                            logger.info(
                                "Dashboard uploaded to %s",
                                publish_result.public_url,
                            )
                        else:
                            logger.info("Dashboard uploaded to R2")

                latest_results = alert_all + cached_dashboard_results
                campground_metrics = [
                    CampgroundMetric.from_entry(
                        availability.entry,
                        # Stable config-file index so Prometheus series survive
                        # alert-flag changes; positional fallback for entries
                        # built outside load_config (tests, ad-hoc callers).
                        config_index=availability.entry.get("_config_index", index),
                        available=availability.available,
                        available_sites=availability.total_sites,
                        scan_success=availability.search_succeeded,
                    )
                    for index, availability in enumerate(latest_results)
                ]
                scan_status.update(
                    entries_count=len(current_entries),
                    available_entries_count=sum(
                        availability.available for availability in latest_results
                    ),
                    available_sites_count=sum(
                        availability.total_sites for availability in latest_results
                    ),
                    campgrounds=campground_metrics,
                    duration_seconds=monotonic() - scan_started,
                    # These totals span the alert tier plus the cached
                    # dashboard results, so they only describe every
                    # configured entry once a dashboard snapshot exists.
                    results_complete=dashboard_snapshot_ready,
                )

                # Free scan-local data before sleeping
                del (
                    alert_keys,
                    alert_found,
                    alert_all,
                    current_entries,
                    campground_metrics,
                    latest_results,
                )

            except DateWindowExhausted as exc:
                logger.error("Search window exhausted (%s); stopping the polling loop.", exc)
                break
            except Exception as exc:
                logger.error("Scan #%d failed: %s", scan_num, exc, exc_info=True)
                # Record the error without zeroing the last known gauges so a
                # transient failure doesn't report 0 monitored campgrounds.
                scan_status.update(
                    duration_seconds=monotonic() - scan_started,
                    error=True,
                )

            _maybe_collect_garbage(scan_num, gc_interval)

            if dashboard_future is None:
                mins_since_dash = (monotonic() - last_dashboard_started) / 60
                dashboard_status = f"in ~{max(0, dashboard_interval - mins_since_dash):.0f} min"
            else:
                dashboard_status = "in progress"
            now = monotonic()
            next_alert_deadline = _advance_poll_deadline(
                next_alert_deadline,
                alert_period_seconds,
                now,
            )
            sleep_seconds = max(0.0, next_alert_deadline - monotonic())
            logger.info(
                "Next alert check in %.1f min. Dashboard scan %s. Ctrl+C to stop.",
                sleep_seconds / 60,
                dashboard_status,
            )
            time.sleep(sleep_seconds)

    except KeyboardInterrupt:
        logger.info("\U0001f3d5  Stopped.")
    finally:
        dashboard_executor.shutdown(wait=False, cancel_futures=True)
        shutdown_dispatcher(wait=False)


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger("camply").setLevel(logging.INFO)

    entries, config = load_config(args.config)
    day_filter = resolve_day_filter(args, config)
    tg_token, tg_chat_id = get_telegram_creds(args, config)

    try:
        compute_date_range(args)
    except DateWindowExhausted as exc:
        sys.exit(f"Error: {exc}")

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
        try:
            current_keys, found_entries, _ = run_once(
                entries,
                args,
                day_filter,
                tg_token,
                tg_chat_id,
                dashboard_path=dashboard_path,
            )
        finally:
            shutdown_dispatcher(wait=False)
        _send_notifications(found_entries, tg_token, tg_chat_id)

        if dashboard_path:
            from .upload import R2Uploader, get_r2_config

            r2_config = get_r2_config(args, config)
            if r2_config:
                upload_result = R2Uploader(r2_config).upload(dashboard_path)
                if upload_result.success:
                    if upload_result.public_url:
                        logger.info("Dashboard uploaded to %s", upload_result.public_url)
                    else:
                        logger.info("Dashboard uploaded to R2")
