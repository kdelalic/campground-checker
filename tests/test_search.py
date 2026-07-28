"""Tests for batched search planning and execution."""

from datetime import date, timedelta
from types import SimpleNamespace

from camply.containers import SearchWindow

from campsite_checker.dispatch import PRIORITY_ALERT, PRIORITY_DASHBOARD
from campsite_checker.providers import PROVIDER_MAP
from campsite_checker.providers.recreation_gov import NativeSearchRecreationDotGov
from campsite_checker.search import (
    SearchOutcome,
    _requires_recreation_area,
    _supports_request_priority,
    build_search_batches,
    build_searcher,
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

        def fake_search(entry, search_window, args, priority=PRIORITY_ALERT):
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

        def fake_search(entry, search_window, args, priority=PRIORITY_ALERT):
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

        def fake_search(entry, search_window, args, priority=PRIORITY_ALERT):
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


class NativeStyleSearch:
    """Stand-in for the native client: declares ``request_priority``."""

    def __init__(self, search_window, weekends_only, nights, request_priority=0, **kwargs):
        self.request_priority = request_priority
        self.kwargs = kwargs


class CamplyStyleSearch:
    """Stand-in for a camply provider: rejects an unknown kwarg."""

    def __init__(self, search_window, weekends_only, nights, campgrounds=None):
        self.campgrounds = campgrounds


class TestRequestPriorityForwarding:
    """Scan priority reaches request-prioritising searchers only."""

    @staticmethod
    def _build(monkeypatch, search_class, priority):
        monkeypatch.setitem(PROVIDER_MAP, "Fake", search_class)
        return build_searcher(
            {"provider": "Fake", "campground_id": 1},
            make_window(),
            make_args(),
            priority,
        )

    def test_dashboard_priority_reaches_native_constructor(self, monkeypatch):
        searcher = self._build(monkeypatch, NativeStyleSearch, PRIORITY_DASHBOARD)

        assert searcher.request_priority == PRIORITY_DASHBOARD

    def test_alert_priority_reaches_native_constructor(self, monkeypatch):
        searcher = self._build(monkeypatch, NativeStyleSearch, PRIORITY_ALERT)

        assert searcher.request_priority == PRIORITY_ALERT

    def test_priority_is_not_passed_to_camply_style_constructor(self, monkeypatch):
        # A TypeError here would mean the unsupported kwarg leaked through.
        searcher = self._build(monkeypatch, CamplyStyleSearch, PRIORITY_DASHBOARD)

        assert searcher.campgrounds == [1]

    def test_native_recreation_client_declares_request_priority(self):
        assert _supports_request_priority(NativeSearchRecreationDotGov) is True
        assert _supports_request_priority(CamplyStyleSearch) is False

    def test_real_providers_are_introspected_as_expected(self):
        """Guards the two constructor contracts ``build_searcher`` depends on.

        ``GoingToCamp`` is camply's, and declares ``recreation_area`` without a
        default; dropping the introspection would stop campground-only entries
        from getting ``recreation_area=[]``. The native clients declare
        ``request_priority``, which is how alert scans outrank dashboard scans
        inside a provider's request gate.
        """
        expected = {
            "RecreationDotGov": (False, True),
            "Yellowstone": (False, False),
            "GoingToCamp": (True, False),
            "ReserveCalifornia": (False, True),
        }
        actual = {
            name: (
                _requires_recreation_area(search_class),
                _supports_request_priority(search_class),
            )
            for name, search_class in PROVIDER_MAP.items()
        }

        assert actual == expected


def test_constructor_introspection_is_cached():
    class RequiredRecreationArea:
        def __init__(self, recreation_area):
            self.recreation_area = recreation_area

    _requires_recreation_area.cache_clear()

    assert _requires_recreation_area(RequiredRecreationArea) is True
    assert _requires_recreation_area(RequiredRecreationArea) is True
    assert _requires_recreation_area.cache_info().hits == 1
