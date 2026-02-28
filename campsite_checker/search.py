import concurrent.futures
import inspect
import logging
import time

from camply.containers import AvailableCampsite, SearchWindow

from .providers import PROVIDER_DISPLAY, PROVIDER_MAP
from .results import get_facility_name

logger = logging.getLogger(__name__)


def build_searcher(entry: dict, search_window: SearchWindow, args):
    provider_name = entry.get("provider", "RecreationDotGov")
    search_class = PROVIDER_MAP[provider_name]

    nights = args.nights if args.nights is not None else entry.get("nights", 1)
    weekends_only = args.weekends_only or entry.get("weekends_only", False)

    kwargs: dict = dict(
        search_window=search_window,
        weekends_only=weekends_only,
        nights=nights,
    )

    if entry.get("campground_id"):
        cid = entry["campground_id"]
        kwargs["campgrounds"] = cid if isinstance(cid, list) else [cid]

    if entry.get("recreation_area"):
        ra = entry["recreation_area"]
        kwargs["recreation_area"] = ra if isinstance(ra, list) else [ra]
    else:
        # Some providers (e.g. SearchReserveCalifornia) declare recreation_area
        # as a required positional arg with no default; pass [] when only
        # campground_id is given so the constructor doesn't raise TypeError.
        param = inspect.signature(search_class.__init__).parameters.get(
            "recreation_area"
        )
        if param is not None and param.default is inspect.Parameter.empty:
            kwargs["recreation_area"] = []

    if entry.get("campsite_id"):
        sid = entry["campsite_id"]
        kwargs["campsites"] = sid if isinstance(sid, list) else [sid]

    return search_class(**kwargs)


def run_search(
    entry: dict, searcher, verbose: bool
) -> tuple[list[AvailableCampsite], str | None]:
    try:
        results = searcher.get_matching_campsites(
            log=verbose,
            verbose=verbose,
            continuous=False,
        )
        return results or [], None
    except Exception as exc:
        label = entry.get("campground_id") or entry.get("recreation_area") or "unknown"
        return [], f"  [WARNING] Search failed for campground {label}: {exc}"


def search_entry(
    entry: dict, search_window: SearchWindow, args
) -> tuple[dict, list[AvailableCampsite], str | None, float]:
    """Build and run a single campsite search. Safe to call from a thread.

    Returns (entry, results, error, elapsed_seconds).
    """
    start = time.monotonic()
    label = entry.get("campground_id") or entry.get("recreation_area") or "unknown"
    try:
        searcher = build_searcher(entry, search_window, args)
        if "name" not in entry and "_resolved_name" not in entry:
            cgs = getattr(searcher, "campgrounds", [])
            if cgs and isinstance(cgs, list):
                if len(cgs) == 1:
                    cg = cgs[0]
                    facility = getattr(cg, "facility_name", "") or ""
                    rec_area = getattr(cg, "recreation_area", "") or ""
                    if rec_area and facility:
                        entry["_resolved_name"] = f"{rec_area} — {facility}"
                    elif rec_area:
                        entry["_resolved_name"] = rec_area
                    elif facility:
                        entry["_resolved_name"] = facility
                elif len(cgs) > 1:
                    rec_area = getattr(cgs[0], "recreation_area", "") or ""
                    if rec_area:
                        entry["_resolved_name"] = f"{rec_area} ({len(cgs)} campgrounds)"
    except Exception as exc:
        return entry, [], f"[ERROR] Could not create searcher for campground {label}: {exc}", time.monotonic() - start
    results, error = run_search(entry, searcher, verbose=args.verbose)
    return entry, results, error, time.monotonic() - start


def execute_searches(
    entries: list[dict], search_window: SearchWindow, args
) -> dict[int, tuple[dict, list[AvailableCampsite], str | None]]:
    """Run all searches in parallel. Returns results keyed by entry index."""
    results_by_index: dict[int, tuple[dict, list[AvailableCampsite], str | None]] = {}

    # Limit max_workers to avoid OOM on smaller container instances
    max_workers = min(getattr(args, 'workers', 2), len(entries))
    logger.info("\u23f3 Starting %d searches (parallelism: %d)...", len(entries), max_workers)

    ok_count = 0
    fail_count = 0
    total_start = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        search_delay = getattr(args, 'search_delay', 0.0)
        future_to_index = {}
        for i, entry in enumerate(entries):
            future = executor.submit(search_entry, entry, search_window, args)
            future_to_index[future] = i
            if search_delay > 0 and i < len(entries) - 1:
                time.sleep(search_delay)
        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            try:
                entry, results, error, elapsed = future.result()
            except Exception as exc:
                entry = entries[i]
                results = []
                error = f"[ERROR] Unexpected crash during search: {exc}"
                elapsed = 0.0

            if entry.get("name"):
                name = entry["name"]
            elif entry.get("_resolved_name"):
                name = entry["_resolved_name"]
            else:
                name = get_facility_name(results) if results else f"campground {entry.get('campground_id', entry.get('recreation_area', '?'))}"
            provider = entry.get("provider", "RecreationDotGov")
            provider_label = PROVIDER_DISPLAY.get(provider, provider.lower())
            if error:
                fail_count += 1
                suffix = " [ERROR]"
            else:
                ok_count += 1
                suffix = ""
            logger.info("\u21b3 %s (%s) \u2014 %.1fs%s", name, provider_label, elapsed, suffix)
            if error and error.startswith("[WARNING]"):
                logger.warning(error.strip())
            results_by_index[i] = (entry, results, error)

    total_elapsed = time.monotonic() - total_start
    logger.info(
        "Searches complete: %d ok, %d failed (%.1fs total)",
        ok_count, fail_count, total_elapsed,
    )

    return results_by_index
