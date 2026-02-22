import concurrent.futures
import inspect
import sys
import time
from typing import Dict, List, Optional, Tuple

from camply.containers import AvailableCampsite, SearchWindow

from .providers import PROVIDER_DISPLAY, PROVIDER_MAP
from .results import get_facility_name


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
) -> Tuple[List[AvailableCampsite], Optional[str]]:
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
) -> Tuple[dict, List[AvailableCampsite], Optional[str]]:
    """Build and run a single campsite search. Safe to call from a thread."""
    label = entry.get("campground_id") or entry.get("recreation_area") or "unknown"
    try:
        searcher = build_searcher(entry, search_window, args)
    except Exception as exc:
        return entry, [], f"[ERROR] Could not create searcher for campground {label}: {exc}"
    results, error = run_search(entry, searcher, verbose=args.verbose)
    return entry, results, error


def execute_searches(
    entries: List[dict], search_window: SearchWindow, args
) -> Dict[int, Tuple[dict, List[AvailableCampsite], Optional[str]]]:
    """Run all searches in parallel. Returns results keyed by entry index."""
    results_by_index: Dict[int, Tuple[dict, List[AvailableCampsite], Optional[str]]] = {}

    # Limit max_workers to avoid OOM on smaller container instances
    max_workers = min(getattr(args, 'workers', 2), len(entries))
    print(f"   \u23f3 Starting {len(entries)} searches (parallelism: {max_workers})...")
    sys.stdout.flush()

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
                entry, results, error = future.result()
            except Exception as exc:
                entry = entries[i]
                results = []
                error = f"[ERROR] Unexpected crash during search: {exc}"
            
            name = get_facility_name(results) if results else f"campground {entry.get('campground_id', '?')}"
            provider = entry.get("provider", "RecreationDotGov")
            provider_label = PROVIDER_DISPLAY.get(provider, provider.lower())
            suffix = " [ERROR]" if error and error.startswith("[ERROR]") else ""
            print(f"   \u21b3 {name} ({provider_label}){suffix}")
            if error and error.startswith("[WARNING]"):
                 print(f"     {error.strip()}")
            sys.stdout.flush()
            results_by_index[i] = (entry, results, error)

    return results_by_index
