import concurrent.futures
import inspect
import logging
import statistics
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from camply.containers import AvailableCampsite, SearchWindow

from .providers import PROVIDER_DISPLAY, PROVIDER_MAP
from .results import get_facility_name
from .throttle import PROVIDER_THROTTLES, RateLimitDetection, detect_rate_limit

logger = logging.getLogger(__name__)


class SearchMetadataCache:
    """Thread-safe bounded cache for stable Camply campsite metadata."""

    def __init__(
        self,
        max_entries: int = 32,
        ttl_seconds: float = 24 * 60 * 60,
        clock=time.monotonic,
    ):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[
            tuple[str, tuple[str, ...]],
            tuple[float, Any],
        ] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(provider: str, searcher) -> tuple[str, tuple[str, ...]] | None:
        if not hasattr(searcher, "campsite_metadata"):
            return None
        facility_ids = tuple(
            sorted(
                str(campground.facility_id) for campground in getattr(searcher, "campgrounds", [])
            )
        )
        return (provider, facility_ids) if facility_ids else None

    def hydrate(self, provider: str, searcher) -> bool:
        key = self._key(provider, searcher)
        if key is None:
            return False
        with self._lock:
            cached = self._entries.get(key)
            if cached is None:
                return False
            expires_at, metadata = cached
            if expires_at <= self._clock():
                del self._entries[key]
                return False
            self._entries.move_to_end(key)
        searcher.campsite_metadata = metadata
        return True

    def store(self, provider: str, searcher) -> bool:
        key = self._key(provider, searcher)
        metadata = getattr(searcher, "campsite_metadata", None)
        if key is None or metadata is None:
            return False
        with self._lock:
            self._entries[key] = (self._clock() + self.ttl_seconds, metadata)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


SEARCH_METADATA_CACHE = SearchMetadataCache()


@dataclass(slots=True)
class SearchBatch:
    """A bounded group of compatible entries searched by one Camply instance."""

    member_indices: tuple[int, ...]
    entries: tuple[dict, ...]
    search_entry: dict
    provider: str


@dataclass(slots=True)
class SearchOutcome:
    results: list[AvailableCampsite]
    error: str | None
    elapsed: float
    resolved_names: dict[str, str]
    resolved_name: str | None
    metadata_reused: bool | None = None
    rate_limited: bool = False
    retry_after_seconds: float | None = None
    started_at: float = 0


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _hashable_ids(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return (value,)


def _effective_nights(entry: dict, args) -> int:
    return args.nights if args.nights is not None else entry.get("nights", 1)


def _effective_weekends_only(entry: dict, args) -> bool:
    return args.weekends_only or entry.get("weekends_only", False)


@lru_cache(maxsize=None)
def _requires_recreation_area(search_class: type) -> bool:
    param = inspect.signature(search_class.__init__).parameters.get("recreation_area")
    return param is not None and param.default is inspect.Parameter.empty


def _batch_key(entry: dict, args) -> tuple | None:
    """Return a compatibility key, or None when an entry must run alone."""
    campground_id = entry.get("campground_id")
    if campground_id is None or isinstance(campground_id, (list, tuple, set)):
        return None
    if entry.get("campsite_id"):
        return None
    return (
        entry.get("provider", "RecreationDotGov"),
        _effective_nights(entry, args),
        _effective_weekends_only(entry, args),
        _hashable_ids(entry.get("recreation_area")),
    )


def build_search_batches(entries: list[dict], args) -> list[SearchBatch]:
    """Group compatible campground entries into stable, bounded batches."""
    if not entries:
        return []

    batch_size = max(1, int(getattr(args, "batch_size", 4)))
    grouped: dict[tuple, list[tuple[int, dict]]] = defaultdict(list)
    standalone: list[SearchBatch] = []

    for index, entry in enumerate(entries):
        key = _batch_key(entry, args) if batch_size > 1 else None
        if key is None:
            standalone.append(
                SearchBatch(
                    member_indices=(index,),
                    entries=(entry,),
                    search_entry=entry,
                    provider=entry.get("provider", "RecreationDotGov"),
                )
            )
        else:
            grouped[key].append((index, entry))

    batches = standalone
    for members in grouped.values():
        for start in range(0, len(members), batch_size):
            chunk = members[start : start + batch_size]
            indices = tuple(index for index, _entry in chunk)
            chunk_entries = tuple(entry for _index, entry in chunk)
            if len(chunk) == 1:
                search_entry = chunk_entries[0]
            else:
                search_entry = dict(chunk_entries[0])
                search_entry["campground_id"] = [entry["campground_id"] for entry in chunk_entries]
                search_entry.pop("name", None)
                search_entry.pop("_resolved_name", None)
            batches.append(
                SearchBatch(
                    member_indices=indices,
                    entries=chunk_entries,
                    search_entry=search_entry,
                    provider=search_entry.get("provider", "RecreationDotGov"),
                )
            )

    batches.sort(key=lambda batch: batch.member_indices[0])
    return batches


def build_searcher(entry: dict, search_window: SearchWindow, args):
    provider_name = entry.get("provider", "RecreationDotGov")
    search_class = PROVIDER_MAP[provider_name]

    kwargs: dict = dict(
        search_window=search_window,
        weekends_only=_effective_weekends_only(entry, args),
        nights=_effective_nights(entry, args),
    )

    if entry.get("campground_id"):
        kwargs["campgrounds"] = _as_list(entry["campground_id"])

    if entry.get("recreation_area"):
        kwargs["recreation_area"] = _as_list(entry["recreation_area"])
    else:
        # Some providers (e.g. SearchReserveCalifornia) declare recreation_area
        # as a required positional arg with no default; pass [] when only
        # campground_id is given so the constructor doesn't raise TypeError.
        if _requires_recreation_area(search_class):
            kwargs["recreation_area"] = []

    if entry.get("campsite_id"):
        kwargs["campsites"] = _as_list(entry["campsite_id"])

    return search_class(**kwargs)


def run_search(
    entry: dict,
    searcher,
    verbose: bool,
) -> tuple[list[AvailableCampsite], str | None, RateLimitDetection]:
    try:
        results = searcher.get_matching_campsites(
            log=verbose,
            verbose=verbose,
            continuous=False,
        )
        return results or [], None, RateLimitDetection(rate_limited=False)
    except Exception as exc:
        label = entry.get("campground_id") or entry.get("recreation_area") or "unknown"
        return (
            [],
            f"[WARNING] Search failed for campground {label}: {exc}",
            detect_rate_limit(exc),
        )


def _facility_label(campground) -> str | None:
    facility = getattr(campground, "facility_name", "") or ""
    recreation_area = getattr(campground, "recreation_area", "") or ""
    if recreation_area and facility:
        return f"{recreation_area} — {facility}"
    return recreation_area or facility or None


def _resolved_searcher_names(searcher) -> tuple[dict[str, str], str | None]:
    campgrounds = getattr(searcher, "campgrounds", [])
    if not campgrounds or not isinstance(campgrounds, list):
        return {}, None

    names = {
        str(campground.facility_id): label
        for campground in campgrounds
        if (label := _facility_label(campground))
    }
    if len(campgrounds) == 1:
        return names, _facility_label(campgrounds[0])
    recreation_area = getattr(campgrounds[0], "recreation_area", "") or ""
    aggregate = f"{recreation_area} ({len(campgrounds)} campgrounds)" if recreation_area else None
    return names, aggregate


def _search_payload(entry: dict, search_window: SearchWindow, args) -> SearchOutcome:
    start = time.monotonic()
    label = entry.get("campground_id") or entry.get("recreation_area") or "unknown"
    try:
        searcher = build_searcher(entry, search_window, args)
        resolved_names, resolved_name = _resolved_searcher_names(searcher)
        metadata_cacheable = hasattr(searcher, "campsite_metadata")
        metadata_hit = SEARCH_METADATA_CACHE.hydrate(
            entry.get("provider", "RecreationDotGov"), searcher
        )
        if metadata_hit:
            logger.debug("Reused cached campsite metadata for %s", label)
    except Exception as exc:
        rate_limit = detect_rate_limit(exc)
        return SearchOutcome(
            results=[],
            error=f"[ERROR] Could not create searcher for campground {label}: {exc}",
            elapsed=time.monotonic() - start,
            resolved_names={},
            resolved_name=None,
            rate_limited=rate_limit.rate_limited,
            retry_after_seconds=rate_limit.retry_after_seconds,
            started_at=start,
        )
    results, error, rate_limit = run_search(entry, searcher, verbose=args.verbose)
    SEARCH_METADATA_CACHE.store(entry.get("provider", "RecreationDotGov"), searcher)
    return SearchOutcome(
        results=results,
        error=error,
        elapsed=time.monotonic() - start,
        resolved_names=resolved_names,
        resolved_name=resolved_name,
        metadata_reused=metadata_hit if metadata_cacheable else None,
        rate_limited=rate_limit.rate_limited,
        retry_after_seconds=rate_limit.retry_after_seconds,
        started_at=start,
    )


def search_entry(
    entry: dict, search_window: SearchWindow, args
) -> tuple[dict, list[AvailableCampsite], str | None, float]:
    """Build and run one campsite search. Retained for direct callers and tests."""
    outcome = _search_payload(entry, search_window, args)
    if "name" not in entry and "_resolved_name" not in entry and outcome.resolved_name:
        entry["_resolved_name"] = outcome.resolved_name
    return entry, outcome.results, outcome.error, outcome.elapsed


def _partition_results(
    batch: SearchBatch,
    results: list[AvailableCampsite],
) -> dict[int, list[AvailableCampsite]]:
    if len(batch.entries) == 1:
        return {batch.member_indices[0]: results}

    indices_by_facility: dict[str, list[int]] = defaultdict(list)
    for index, entry in zip(batch.member_indices, batch.entries, strict=True):
        indices_by_facility[str(entry["campground_id"])].append(index)

    partitioned = {index: [] for index in batch.member_indices}
    unmatched = 0
    for result in results:
        matching_indices = indices_by_facility.get(str(getattr(result, "facility_id", "")), [])
        if not matching_indices:
            unmatched += 1
            continue
        for index in matching_indices:
            partitioned[index].append(result)
    if unmatched:
        logger.warning(
            "Ignored %d batched result(s) without a matching configured facility ID",
            unmatched,
        )
    return partitioned


def _entry_display_name(entry: dict, results: list[AvailableCampsite]) -> str:
    if entry.get("name"):
        return entry["name"]
    if entry.get("_resolved_name"):
        return entry["_resolved_name"]
    if results:
        return get_facility_name(results)
    identifier = entry.get("campground_id", entry.get("recreation_area", "?"))
    return f"campground {identifier}"


def execute_searches(
    entries: list[dict], search_window: SearchWindow, args
) -> dict[int, tuple[dict, list[AvailableCampsite], str | None]]:
    """Run bounded campsite search batches with per-provider submission pacing."""
    results_by_index: dict[int, tuple[dict, list[AvailableCampsite], str | None]] = {}
    batches = build_search_batches(entries, args)
    if not batches:
        return results_by_index
    for provider in {batch.provider for batch in batches}:
        PROVIDER_THROTTLES.ensure(provider)

    requested_workers = max(1, int(getattr(args, "workers", 2)))
    max_workers = min(requested_workers, len(batches))
    search_delay = max(0.0, float(getattr(args, "search_delay", 0.0)))
    logger.info(
        "⏳ Starting %d searches in %d batch(es) "
        "(parallelism: %d, batch size: %d, provider delay: %.1fs)...",
        len(entries),
        len(batches),
        max_workers,
        max(1, int(getattr(args, "batch_size", 4))),
        search_delay,
    )

    ok_count = 0
    fail_count = 0
    total_start = time.monotonic()
    durations_by_provider: dict[str, list[float]] = defaultdict(list)
    entries_by_provider: dict[str, int] = defaultdict(int)
    metadata_reuse_by_provider: dict[str, list[bool]] = defaultdict(list)
    next_submission: dict[str, float] = defaultdict(float)

    def skip_batch(batch: SearchBatch, cooldown_seconds: float) -> None:
        nonlocal fail_count
        error = (
            f"[WARNING] Skipping {batch.provider} search while provider cooldown is active "
            f"({cooldown_seconds:.0f}s remaining)"
        )
        for index, entry in zip(batch.member_indices, batch.entries, strict=True):
            results_by_index[index] = (entry, [], error)
        fail_count += len(batch.entries)

    remaining = []
    skipped_by_provider: dict[str, tuple[int, float]] = {}
    for batch in batches:
        cooldown = PROVIDER_THROTTLES.cooldown_seconds(batch.provider)
        if cooldown <= 0:
            remaining.append(batch)
            continue
        skip_batch(batch, cooldown)
        skipped_count, longest_cooldown = skipped_by_provider.get(batch.provider, (0, 0))
        skipped_by_provider[batch.provider] = (
            skipped_count + len(batch.entries),
            max(longest_cooldown, cooldown),
        )
    for provider, (skipped_count, cooldown) in skipped_by_provider.items():
        logger.warning(
            "Provider %s cooldown active; skipped %d campground(s), retrying next scan "
            "(%.0fs remaining)",
            PROVIDER_DISPLAY.get(provider, provider),
            skipped_count,
            cooldown,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        active: dict[concurrent.futures.Future, SearchBatch] = {}
        while remaining or active:
            now = time.monotonic()
            while len(active) < max_workers:
                ready_index = next(
                    (
                        index
                        for index, batch in enumerate(remaining)
                        if next_submission[batch.provider] <= now
                    ),
                    None,
                )
                if ready_index is None:
                    break
                batch = remaining.pop(ready_index)
                future = executor.submit(_search_payload, batch.search_entry, search_window, args)
                active[future] = batch
                next_submission[batch.provider] = now + search_delay
                now = time.monotonic()

            if not active:
                next_ready = min(next_submission[batch.provider] for batch in remaining)
                time.sleep(max(0.0, next_ready - time.monotonic()))
                continue

            wait_timeout = None
            if remaining and len(active) < max_workers:
                next_ready = min(next_submission[batch.provider] for batch in remaining)
                wait_timeout = max(0.0, next_ready - time.monotonic())
            completed, _pending = concurrent.futures.wait(
                active,
                timeout=wait_timeout,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not completed:
                continue

            for future in completed:
                batch = active.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = SearchOutcome(
                        results=[],
                        error=f"[ERROR] Unexpected crash during search: {exc}",
                        elapsed=0.0,
                        resolved_names={},
                        resolved_name=None,
                    )

                partitions = _partition_results(batch, outcome.results)
                batch_size = len(batch.entries)
                durations_by_provider[batch.provider].append(outcome.elapsed)
                entries_by_provider[batch.provider] += batch_size
                if outcome.metadata_reused is not None:
                    metadata_reuse_by_provider[batch.provider].append(outcome.metadata_reused)
                if outcome.error:
                    fail_count += batch_size
                    suffix = " [ERROR]"
                else:
                    ok_count += batch_size
                    suffix = ""
                    PROVIDER_THROTTLES.record_success(
                        batch.provider,
                        request_started_at=outcome.started_at,
                    )

                provider_label = PROVIDER_DISPLAY.get(batch.provider, batch.provider.lower())
                logger.info(
                    "↳ %s batch (%d campground%s) — %.1fs%s",
                    provider_label,
                    batch_size,
                    "" if batch_size == 1 else "s",
                    outcome.elapsed,
                    suffix,
                )
                if outcome.error and outcome.error.startswith("[WARNING]"):
                    logger.warning(outcome.error)
                if outcome.rate_limited:
                    delay = PROVIDER_THROTTLES.record_rate_limit(
                        batch.provider,
                        retry_after_seconds=outcome.retry_after_seconds,
                    )
                    queued_for_provider = [
                        queued for queued in remaining if queued.provider == batch.provider
                    ]
                    remaining = [
                        queued for queued in remaining if queued.provider != batch.provider
                    ]
                    for queued in queued_for_provider:
                        skip_batch(queued, delay)
                    logger.warning(
                        "Provider %s rate limited; applying %.0fs cooldown and skipping "
                        "%d queued campground(s)",
                        provider_label,
                        delay,
                        sum(len(queued.entries) for queued in queued_for_provider),
                    )

                for index, entry in zip(batch.member_indices, batch.entries, strict=True):
                    if "name" not in entry and "_resolved_name" not in entry:
                        resolved = outcome.resolved_names.get(str(entry.get("campground_id")))
                        if resolved is None and batch_size == 1:
                            resolved = outcome.resolved_name
                        if resolved:
                            entry["_resolved_name"] = resolved
                    entry_results = partitions[index]
                    logger.debug(
                        "%s returned %d result(s)",
                        _entry_display_name(entry, entry_results),
                        len(entry_results),
                    )
                    results_by_index[index] = (
                        entry,
                        entry_results,
                        outcome.error,
                    )

    total_elapsed = time.monotonic() - total_start
    for provider, durations in durations_by_provider.items():
        provider_label = PROVIDER_DISPLAY.get(provider, provider.lower())
        metadata_observations = metadata_reuse_by_provider[provider]
        metadata_label = (
            f", metadata reused {sum(metadata_observations)}/{len(metadata_observations)}"
            if metadata_observations
            else ""
        )
        logger.info(
            "   %s: %d campground(s), %d batch(es), median %.1fs, slowest %.1fs%s",
            provider_label,
            entries_by_provider[provider],
            len(durations),
            statistics.median(durations),
            max(durations),
            metadata_label,
        )
    logger.info(
        "Searches complete: %d ok, %d failed in %d batch(es) (%.1fs total)",
        ok_count,
        fail_count,
        len(batches),
        total_elapsed,
    )
    return results_by_index
