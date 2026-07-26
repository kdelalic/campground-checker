"""Tests for batched search planning and execution."""

from datetime import date, timedelta
from types import SimpleNamespace

from camply.containers import SearchWindow

from campsite_checker.search import (
    SEARCH_METADATA_CACHE,
    SearchMetadataCache,
    SearchOutcome,
    _requires_recreation_area,
    _search_payload,
    build_search_batches,
    execute_searches,
)
from campsite_checker.throttle import ProviderThrottleRegistry

from .conftest import make_campsite


def make_args(**overrides):
    values = {
        "nights": None,
        "weekends_only": False,
        "batch_size": 2,
        "workers": 2,
        "search_delay": 0.0,
        "verbose": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_window():
    start = date.today() + timedelta(days=1)
    return SearchWindow(start_date=start, end_date=start + timedelta(days=7))


class TestBuildSearchBatches:
    def test_groups_compatible_entries_up_to_batch_size(self):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": campground_id}
            for campground_id in (1, 2, 3)
        ]

        batches = build_search_batches(entries, make_args(batch_size=2))

        assert [batch.member_indices for batch in batches] == [(0, 1), (2,)]
        assert batches[0].search_entry["campground_id"] == [1, 2]

    def test_separates_entries_with_different_search_options(self):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": 1, "nights": 1},
            {"provider": "RecreationDotGov", "campground_id": 2, "nights": 2},
            {"provider": "ReserveCalifornia", "campground_id": 3, "nights": 1},
        ]

        batches = build_search_batches(entries, make_args(batch_size=4))

        assert len(batches) == 3
        assert all(len(batch.entries) == 1 for batch in batches)

    def test_global_nights_override_allows_batching(self):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": 1, "nights": 1},
            {"provider": "RecreationDotGov", "campground_id": 2, "nights": 2},
        ]

        batches = build_search_batches(entries, make_args(nights=3, batch_size=4))

        assert len(batches) == 1
        assert batches[0].member_indices == (0, 1)

    def test_going_to_camp_recreation_areas_remain_separate(self):
        entries = [
            {
                "provider": "GoingToCamp",
                "recreation_area": 10,
                "campground_id": 1,
            },
            {
                "provider": "GoingToCamp",
                "recreation_area": 20,
                "campground_id": 2,
            },
        ]

        batches = build_search_batches(entries, make_args(batch_size=4))

        assert len(batches) == 2

    def test_batch_size_one_disables_batching(self):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": 1},
            {"provider": "RecreationDotGov", "campground_id": 2},
        ]

        batches = build_search_batches(entries, make_args(batch_size=1))

        assert len(batches) == 2
        assert all(len(batch.entries) == 1 for batch in batches)


class TestExecuteSearches:
    def test_partitions_batched_results_by_facility(self, monkeypatch):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": campground_id}
            for campground_id in (10, 20, 30)
        ]
        calls = []

        def fake_search(entry, search_window, args):
            campground_ids = entry["campground_id"]
            if not isinstance(campground_ids, list):
                campground_ids = [campground_ids]
            calls.append(tuple(campground_ids))
            results = [
                make_campsite(campsite_id=campground_id, facility_id=campground_id)
                for campground_id in campground_ids
            ]
            return SearchOutcome(results, None, 0.01, {}, None)

        monkeypatch.setattr("campsite_checker.search._search_payload", fake_search)

        results = execute_searches(entries, make_window(), make_args(batch_size=2))

        assert sorted(calls) == [(10, 20), (30,)]
        assert results[0][1][0].facility_id == 10
        assert results[1][1][0].facility_id == 20
        assert results[2][1][0].facility_id == 30

    def test_batch_error_is_reported_for_each_member(self, monkeypatch):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": 10},
            {"provider": "RecreationDotGov", "campground_id": 20},
        ]

        def fake_search(entry, search_window, args):
            return SearchOutcome([], "[WARNING] unavailable", 0.01, {}, None)

        monkeypatch.setattr("campsite_checker.search._search_payload", fake_search)

        results = execute_searches(entries, make_window(), make_args(batch_size=2))

        assert results[0][2] == "[WARNING] unavailable"
        assert results[1][2] == "[WARNING] unavailable"

    def test_empty_entry_list_returns_immediately(self):
        assert execute_searches([], make_window(), make_args()) == {}

    def test_rate_limit_skips_queued_provider_but_continues_others(self, monkeypatch):
        entries = [
            {"provider": "RecreationDotGov", "campground_id": 10},
            {"provider": "RecreationDotGov", "campground_id": 20},
            {"provider": "ReserveCalifornia", "campground_id": 30},
        ]
        calls = []

        def clock():
            return 100.0

        registry = ProviderThrottleRegistry(clock=clock)

        def fake_search(entry, search_window, args):
            calls.append((entry["provider"], entry["campground_id"]))
            if entry["provider"] == "RecreationDotGov":
                return SearchOutcome(
                    results=[],
                    error="[WARNING] 429 Too Many Requests",
                    elapsed=0.01,
                    resolved_names={},
                    resolved_name=None,
                    rate_limited=True,
                    retry_after_seconds=45,
                    started_at=99,
                )
            return SearchOutcome(
                results=[],
                error=None,
                elapsed=0.01,
                resolved_names={},
                resolved_name=None,
                started_at=100,
            )

        monkeypatch.setattr("campsite_checker.search.PROVIDER_THROTTLES", registry)
        monkeypatch.setattr("campsite_checker.search._search_payload", fake_search)

        results = execute_searches(
            entries,
            make_window(),
            make_args(batch_size=1, workers=1),
        )

        assert calls == [
            ("RecreationDotGov", 10),
            ("ReserveCalifornia", 30),
        ]
        assert "cooldown is active" in results[1][2]
        assert results[2][2] is None
        snapshot = {item.provider: item for item in registry.snapshot()}
        assert snapshot["RecreationDotGov"].rate_limit_events == 1
        assert snapshot["RecreationDotGov"].cooldown_seconds == 45

        calls.clear()
        next_results = execute_searches(
            entries,
            make_window(),
            make_args(batch_size=1, workers=1),
        )

        assert calls == [("ReserveCalifornia", 30)]
        assert "cooldown is active" in next_results[0][2]
        assert "cooldown is active" in next_results[1][2]


class TestSearchMetadataCache:
    def test_hydrates_matching_searcher(self):
        cache = SearchMetadataCache()
        metadata = object()
        campground = SimpleNamespace(facility_id=123)
        source = SimpleNamespace(
            campgrounds=[campground],
            campsite_metadata=metadata,
        )
        target = SimpleNamespace(
            campgrounds=[campground],
            campsite_metadata=None,
        )

        assert cache.store("RecreationDotGov", source) is True
        assert cache.hydrate("RecreationDotGov", target) is True
        assert target.campsite_metadata is metadata

    def test_is_bounded_and_evicts_oldest_entry(self):
        cache = SearchMetadataCache(max_entries=1)
        first = SimpleNamespace(
            campgrounds=[SimpleNamespace(facility_id=1)],
            campsite_metadata=object(),
        )
        second = SimpleNamespace(
            campgrounds=[SimpleNamespace(facility_id=2)],
            campsite_metadata=object(),
        )

        cache.store("RecreationDotGov", first)
        cache.store("RecreationDotGov", second)

        assert len(cache) == 1
        assert (
            cache.hydrate(
                "RecreationDotGov",
                SimpleNamespace(
                    campgrounds=first.campgrounds,
                    campsite_metadata=None,
                ),
            )
            is False
        )

    def test_ignores_searchers_without_metadata_support(self):
        cache = SearchMetadataCache()
        searcher = SimpleNamespace(
            campgrounds=[SimpleNamespace(facility_id=1)],
        )

        assert cache.store("Yellowstone", searcher) is False
        assert cache.hydrate("Yellowstone", searcher) is False

    def test_expired_metadata_is_not_reused(self):
        now = [100.0]
        cache = SearchMetadataCache(
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        campground = SimpleNamespace(facility_id=1)
        cache.store(
            "RecreationDotGov",
            SimpleNamespace(
                campgrounds=[campground],
                campsite_metadata=object(),
            ),
        )
        now[0] = 161.0

        assert (
            cache.hydrate(
                "RecreationDotGov",
                SimpleNamespace(
                    campgrounds=[campground],
                    campsite_metadata=None,
                ),
            )
            is False
        )
        assert len(cache) == 0


def test_constructor_introspection_is_cached():
    class RequiredRecreationArea:
        def __init__(self, recreation_area):
            self.recreation_area = recreation_area

    _requires_recreation_area.cache_clear()

    assert _requires_recreation_area(RequiredRecreationArea) is True
    assert _requires_recreation_area(RequiredRecreationArea) is True
    assert _requires_recreation_area.cache_info().hits == 1


def test_search_payload_reuses_cached_metadata(monkeypatch):
    campground = SimpleNamespace(
        facility_id=123,
        facility_name="Test Campground",
        recreation_area="Test Area",
    )
    searchers = []
    metadata_fetches = []

    def fake_build_searcher(entry, search_window, args):
        searcher = SimpleNamespace(
            campgrounds=[campground],
            campsite_metadata=None,
        )
        searchers.append(searcher)
        return searcher

    def fake_run_search(entry, searcher, verbose):
        if searcher.campsite_metadata is None:
            metadata_fetches.append(1)
            searcher.campsite_metadata = object()
        from campsite_checker.throttle import RateLimitDetection

        return [], None, RateLimitDetection(rate_limited=False)

    SEARCH_METADATA_CACHE.clear()
    monkeypatch.setattr("campsite_checker.search.build_searcher", fake_build_searcher)
    monkeypatch.setattr("campsite_checker.search.run_search", fake_run_search)
    entry = {"provider": "RecreationDotGov", "campground_id": 123}

    first = _search_payload(entry, make_window(), make_args())
    second = _search_payload(entry, make_window(), make_args())

    assert len(searchers) == 2
    assert len(metadata_fetches) == 1
    assert searchers[1].campsite_metadata is searchers[0].campsite_metadata
    assert first.metadata_reused is False
    assert second.metadata_reused is True
    SEARCH_METADATA_CACHE.clear()
