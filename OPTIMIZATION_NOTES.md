# Campground Checker Optimization Notes

This document records optimization opportunities identified during the July 2026
code review. It is intended to be updated as changes are benchmarked and
implemented.

## Current Performance Profile

The application is primarily network-bound. Compatible campsite entries now
share bounded Camply search batches, while entries with incompatible constraints
run separately. Local filtering, normalization, formatting, dashboard
generation, notification processing, and persistence happen after the remote
searches complete.

The current `campsites.yaml` contains 34 campground entries. Most entries share
the same provider-level search settings, which creates opportunities to improve
scheduling and reduce repeated setup.

## Implementation Status

Items 1–7 were implemented in July 2026:

- Search submission delay is now applied per provider while completed work is
  consumed continuously. The Docker default delay changed from two seconds to
  zero.
- The default worker count is four, with per-provider batch timing metrics
  logged after every scan.
- Compatible campgrounds are grouped into bounded batches of four by default.
  `--batch-size 1` disables batching for comparison or troubleshooting.
- Search results are filtered, deduplicated, grouped, named, and keyed into a
  shared `ProcessedAvailability` model. Terminal, Telegram, and dashboard
  consumers reuse that model.
- Stable Recreation.gov campsite metadata uses a 32-entry, 24-hour,
  thread-safe LRU cache. R2 uploads reuse one boto3 client per process.
- Semantic fingerprints prevent unchanged dashboard writes and uploads.
  Failed uploads remain eligible for retry, and unchanged sent-key state is not
  rewritten.
- Full garbage collection runs every 12 scans by default and reports duration,
  collected objects, generation counts, and memory. Polling uses anchored
  deadlines to avoid accumulating scan-duration drift.

Targeted tests cover batch compatibility, batch bounds, result partitioning,
batch errors, CLI tuning defaults, deduplication, and normalized grouping.

## Prioritized Recommendations

### 1. Remove or redesign the global search delay

**Impact:** High  
**Status:** Implemented

`execute_searches()` now maintains a bounded active-future set and consumes work
as soon as it completes. `--search-delay` establishes a minimum interval between
submissions to the same provider, so work for another provider can start without
waiting behind that interval.

The CLI and Docker defaults are both zero seconds. Increase the delay only if
scan metrics show provider throttling or transient failures.

### 2. Tune worker count using measurements

**Impact:** High  
**Status:** Instrumented; deployment benchmarking remains

The CLI and Docker image now default to four workers. Searches are network-bound,
so additional workers may reduce wall-clock scan time, while Camply's pandas
processing and provider limits establish a practical upper bound.

Every scan logs campground count, batch count, median batch duration, slowest
batch duration, failures, and total duration by provider.

Benchmark worker counts such as 2, 3, 4, 6, and 8. For each run, record:

- Total scan duration
- Per-provider request duration
- Error and throttling counts
- Peak resident memory
- CPU utilization
- Number of results

Do not select a higher default based only on a faster successful run. Confirm
that it remains reliable across several scans and does not trigger provider rate
limits.

### 3. Batch compatible campground searches

**Impact:** Potentially high  
**Status:** Implemented with a configurable bounded batch size

`build_search_batches()` groups entries by compatible settings:

- Provider
- Number of nights
- Weekend-only setting
- Campsite or recreation-area constraints

The default maximum is four campgrounds per batch. `--batch-size` controls the
bound, and a value of one disables batching. Entries with explicit campsite IDs,
multi-ID campground definitions, or incompatible recreation areas remain
standalone.

Results are partitioned back to every original entry using `facility_id`.
Failures are propagated to each member of the affected bounded batch.

Batching may reduce searcher construction, metadata lookup, session, and
DataFrame overhead. It may not reduce the underlying availability requests if a
provider still processes each campground serially, so this change must be
benchmarked rather than assumed to be faster.

### 4. Normalize results once per campground

**Impact:** Medium  
**Status:** Implemented

After task-specific day filtering, `runner.py` builds one normalized result per
configured entry:

```python
@dataclass(frozen=True, slots=True)
class ProcessedAvailability:
    entry: dict
    campsites: tuple[AvailableCampsite, ...]
    facility_name: str
    booking_url: str
    campsite_ids_by_date: dict[date, frozenset[int | str]]
    notification_keys: frozenset[tuple[str, int | str, date]]
    total_sites: int
```

Terminal formatting, Telegram notifications, and dashboard generation consume
this representation directly. Backward-compatible helpers still accept raw
Camply results for tests and callers outside the main scan path.

### 5. Reuse long-lived clients and stable metadata

**Impact:** Medium  
**Status:** Implemented

The search layer retains Recreation.gov campsite metadata by provider and exact
facility-ID set. The cache is thread-safe, limited to 32 entries, and expires
entries after 24 hours so long-running processes periodically refresh provider
metadata.

`R2Uploader` lazily constructs one boto3 client and reuses its HTTP connection
pool for subsequent uploads. Mutable Camply searcher objects are deliberately
not cached because they retain results, search-window state, and pandas data.

### 6. Skip unchanged writes and uploads

**Impact:** Medium for hosted deployments  
**Status:** Implemented

`DashboardPublisher` hashes dashboard-relevant configuration and availability
without including the generated timestamp. Unchanged state skips both HTML
generation and R2 upload. Upload success has its own fingerprint, so a failed
upload is retried on the next scan without rewriting unchanged HTML.

Sent-key JSON is serialized deterministically and written only when its pruned
content changes.

### 7. Measure forced garbage collection

**Impact:** Unknown  
**Status:** Implemented with a reduced default frequency

Full collection now runs every 12 scans rather than every scan. Each collection
logs elapsed time, collected object count, generation counts, and current Linux
RSS or peak RSS on other supported Unix platforms. `--gc-interval 0` disables
forced collection for comparison.

## Smaller Opportunities

Completed smaller changes:

- Provider constructor introspection is cached.
- Weekday display labels are precomputed.
- R2 clients and connection pools are reused.
- Deadline-based polling prevents cumulative schedule drift and skips missed
  periods after overruns.

Separating the dashboard's static CSS and JavaScript remains low priority because
semantic fingerprinting now skips the entire rendering path when availability
is unchanged.

## Suggested Implementation Order

The implementation phase is complete. The remaining work is operational:

1. Benchmark worker and batch sizes on the deployment host.
2. Compare GC metrics with the default interval and with forced GC disabled.
3. Monitor metadata-cache hit logs, provider throttling, and upload retry rates.
4. Revisit static dashboard template extraction only if changed-state rendering
   appears in a profile.

## Benchmark Checklist

Use the same campground configuration, date range, host, and time window when
comparing variants.

Record at minimum:

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Entry count | | |
| Search task count | | |
| Worker count | | |
| Search delay | | |
| Total scan time | | |
| Median task time | | |
| Slowest task time | | |
| Peak RSS | | |
| Search failures | | |
| Throttled requests | | |
| Dashboard generation time | | |
| Dashboard upload time | | |
| Garbage-collection time | | |

Run each candidate several times. Compare medians and worst cases rather than a
single result.

## Validation Status

The implementation is covered by the full pytest suite and the repository's
Ruff linting and formatting checks. Live provider benchmarking remains necessary
to tune worker count and batch size for the deployment environment.
