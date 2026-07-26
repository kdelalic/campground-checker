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

Items 1–4 were implemented in July 2026:

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
**Effort:** Medium

Every scan reconstructs Camply searchers. In addition,
`campsite_checker/upload.py:53-69` creates a new boto3 S3 client for every
dashboard upload.

Potential improvements:

- Cache stable campground metadata by `(provider, campground_id)`.
- Reuse the boto3 client and its HTTP connection pool.
- Investigate whether Camply exposes reusable provider clients or sessions.

Do not cache entire Camply searchers until their lifecycle is understood.
Searchers may retain mutable results, search-window state, sessions, or large
pandas objects.

### 6. Skip unchanged writes and uploads

**Impact:** Medium for hosted deployments  
**Effort:** Low to medium

In forever mode, `campsite_checker/runner.py:352-365` regenerates and uploads the
dashboard after every alert scan. The sent-key file is also rewritten every
iteration.

Compute a stable hash of the semantic availability state. When it has not
changed:

- Skip the R2 upload.
- Skip rewriting the sent-key file.
- Optionally skip rebuilding the dashboard.

The dashboard currently includes a generated timestamp. Exclude that timestamp
from the semantic hash, or decide explicitly whether each scan timestamp must be
published even when availability is unchanged.

### 7. Measure forced garbage collection

**Impact:** Unknown  
**Effort:** Low

`campsite_checker/runner.py:384-387` calls `gc.collect()` after every scan. A full
collection can introduce pauses, and it does not guarantee that Python returns
memory to the operating system.

Instrument:

- Time spent inside `gc.collect()`
- Objects collected
- RSS immediately before and after collection
- Long-term RSS with and without forced collection

Keep forced collection only if measurements show that it controls meaningful
memory growth.

## Smaller Opportunities

These changes are lower priority because network time should dominate:

- Cache whether each provider constructor requires an explicit
  `recreation_area` argument instead of calling `inspect.signature()` for every
  searcher construction.
- Precompute the inverse weekday-name mapping used by scan-header formatting.
- Reuse the R2 client even if unchanged-upload detection is not implemented.
- Use deadline-based polling so scan duration does not continually shift the
  intended schedule.
- Separate the dashboard's static CSS and JavaScript template from dynamic card
  data to reduce repeated string construction.

## Suggested Implementation Order

1. Add performance instrumentation.
2. Benchmark `SEARCH_DELAY=0`.
3. Benchmark worker counts.
4. Normalize results once and update all consumers.
5. Reuse the R2 client and skip unchanged persistence.
6. Prototype bounded provider batching.
7. Evaluate forced garbage collection using recorded memory data.

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
