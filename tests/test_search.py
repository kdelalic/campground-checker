"""Tests for batched search planning and execution."""

from datetime import date, timedelta
from types import SimpleNamespace

from camply.containers import SearchWindow

from campsite_checker.search import (
    SearchOutcome,
    build_search_batches,
    execute_searches,
)

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
