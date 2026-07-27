"""Contract and retry tests for the native Recreation.gov search."""

import json
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests
from camply.containers import CampgroundFacility, SearchWindow
from camply.providers import RecreationDotGov

from campsite_checker.providers.recreation_gov import (
    DEFAULT_REQUEST_TIMEOUT,
    FACILITY_IDENTITY_CACHE,
    FacilityIdentityCache,
    IdentityCachedRecreationDotGov,
    NativeSearchRecreationDotGov,
    ProviderRequestMetrics,
    RequestGate,
)
from campsite_checker.results import process_results
from campsite_checker.throttle import detect_rate_limit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "recreation_gov_availability.json"
PRIORITY_ALERT = 0
PRIORITY_DASHBOARD = 1


def make_facility(
    facility_id=232447,
    name="Fixture Campground",
    area="Fixture National Park",
    recreation_area_id=2991,
):
    return CampgroundFacility(
        facility_name=name,
        recreation_area=area,
        facility_id=facility_id,
        recreation_area_id=recreation_area_id,
        map_id=None,
        coordinates=None,
    )


@pytest.fixture(autouse=True)
def clear_identity_cache():
    FACILITY_IDENTITY_CACHE.clear()
    yield
    FACILITY_IDENTITY_CACHE.clear()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} response",
                response=self,
            )

    def json(self):
        return self._payload


class QueueSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeIdentityProvider:
    def find_campgrounds(self, **_kwargs):
        return [make_facility()]


class ImmediateGate:
    """Records slot priorities and deferrals without any real waiting."""

    def __init__(self):
        self.priorities = []
        self.deferrals = []

    @contextmanager
    def slot(self, priority=PRIORITY_ALERT):
        self.priorities.append(priority)
        yield self

    def defer(self, seconds):
        self.deferrals.append(seconds)


@pytest.fixture
def availability_payload():
    return json.loads(FIXTURE_PATH.read_text())


def make_search(
    monkeypatch,
    *,
    session,
    nights=1,
    metrics=None,
    campsites=None,
    request_gate=None,
    request_priority=PRIORITY_ALERT,
):
    monkeypatch.setattr(
        NativeSearchRecreationDotGov,
        "provider_class",
        FakeIdentityProvider,
    )
    return NativeSearchRecreationDotGov(
        search_window=SearchWindow(
            start_date=date(2099, 8, 5),
            end_date=date(2099, 8, 9),
        ),
        campgrounds=[232447],
        campsites=campsites,
        nights=nights,
        session=session,
        sleep=lambda _seconds: None,
        request_gate=request_gate or ImmediateGate(),
        request_metrics=metrics or ProviderRequestMetrics(),
        request_priority=request_priority,
    )


def test_fixture_parsing_preserves_downstream_result_contract(
    monkeypatch,
    availability_payload,
):
    search = make_search(
        monkeypatch,
        session=QueueSession(FakeResponse(payload=availability_payload)),
        nights=2,
    )

    results = search.get_matching_campsites(log=False)

    assert len(results) == 1
    result = results[0]
    assert result.campsite_id == 100
    assert result.booking_date.date() == date(2099, 8, 5)
    assert result.booking_end_date.date() == date(2099, 8, 7)
    assert result.booking_nights == 2
    assert result.campsite_site_name == "A100"
    assert result.campsite_loop_name == "Bay View"
    assert result.campsite_type == "STANDARD NONELECTRIC"
    assert result.facility_id == 232447
    assert result.facility_name == "Fixture Campground"
    assert result.recreation_area == "Fixture National Park"
    assert result.booking_url.endswith("/100")
    assert result.campsite_attributes == []

    processed = process_results(
        {"provider": "RecreationDotGov", "campground_id": 232447},
        results,
        day_filter=None,
    )
    assert processed.total_sites == 1
    assert processed.facility_name == "Fixture National Park — Fixture Campground"


def test_unavailable_statuses_and_campsite_filter_are_applied(
    monkeypatch,
    availability_payload,
):
    search = make_search(
        monkeypatch,
        session=QueueSession(FakeResponse(payload=availability_payload)),
        campsites=[200],
    )

    results = search.get_matching_campsites(log=False)

    assert [(result.campsite_id, result.booking_date.date()) for result in results] == [
        (200, date(2099, 8, 6))
    ]


def test_retryable_server_errors_use_short_bounded_retry(monkeypatch):
    metrics = ProviderRequestMetrics()
    sleeps = []
    session = QueueSession(
        FakeResponse(status_code=500),
        FakeResponse(status_code=502),
        FakeResponse(payload={"campsites": {}}),
    )
    search = make_search(monkeypatch, session=session, metrics=metrics)
    search._sleep = sleeps.append

    assert search._request_month(232447, date(2099, 8, 1)) == {"campsites": {}}
    assert sleeps == [1.0, 2.0]
    assert len(session.calls) == 3
    snapshot = metrics.snapshot()[0]
    assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (3, 2, 0)
    assert all(call[1]["timeout"] == DEFAULT_REQUEST_TIMEOUT for call in session.calls)


def test_429_fails_immediately_and_defers_gate_by_retry_after(monkeypatch):
    metrics = ProviderRequestMetrics()
    gate = ImmediateGate()
    response = FakeResponse(status_code=429, headers={"Retry-After": "75"})
    search = make_search(
        monkeypatch,
        session=QueueSession(response),
        metrics=metrics,
        request_gate=gate,
        request_priority=PRIORITY_DASHBOARD,
    )

    with pytest.raises(requests.HTTPError) as exc_info:
        search._request_month(232447, date(2099, 8, 1))

    detection = detect_rate_limit(exc_info.value)
    assert detection.rate_limited is True
    assert detection.retry_after_seconds == 75
    # The pause is applied while the slot is still held, so batches already
    # executing stop issuing requests too.
    assert gate.deferrals == [75]
    assert gate.priorities == [PRIORITY_DASHBOARD]
    snapshot = metrics.snapshot()[0]
    assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (1, 0, 1)


def test_429_without_retry_after_defers_gate_by_default_pause(monkeypatch):
    gate = ImmediateGate()
    search = make_search(
        monkeypatch,
        session=QueueSession(FakeResponse(status_code=429)),
        request_gate=gate,
    )

    with pytest.raises(requests.HTTPError):
        search._request_month(232447, date(2099, 8, 1))

    assert gate.deferrals == [30]


def test_server_errors_do_not_defer_the_shared_gate(monkeypatch):
    gate = ImmediateGate()
    search = make_search(
        monkeypatch,
        session=QueueSession(
            FakeResponse(status_code=500),
            FakeResponse(payload={"campsites": {}}),
        ),
        request_gate=gate,
    )
    search._sleep = lambda _seconds: None

    assert search._request_month(232447, date(2099, 8, 1)) == {"campsites": {}}
    assert gate.deferrals == []


def test_timeouts_stop_after_three_attempts(monkeypatch):
    metrics = ProviderRequestMetrics()
    search = make_search(
        monkeypatch,
        session=QueueSession(
            requests.Timeout("one"),
            requests.Timeout("two"),
            requests.Timeout("three"),
        ),
        metrics=metrics,
    )

    with pytest.raises(requests.Timeout, match="three"):
        search._request_month(232447, date(2099, 8, 1))

    snapshot = metrics.snapshot()[0]
    assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (3, 2, 1)


def test_weekends_only_matches_friday_and_saturday(monkeypatch):
    search = make_search(
        monkeypatch,
        session=QueueSession(FakeResponse(payload={})),
    )
    search.weekends_only = True

    assert {day.weekday() for day in search._search_days()} == {4, 5}


def test_request_gate_spaces_provider_request_starts():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gate = RequestGate(
        max_concurrent=1,
        requests_per_second=2,
        clock=lambda: now[0],
        sleep=sleep,
    )

    with gate:
        pass
    with gate:
        pass

    assert sleeps == [0.5]


def test_request_gate_defer_pauses_all_future_starts():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gate = RequestGate(
        max_concurrent=2,
        requests_per_second=1,
        clock=lambda: now[0],
        sleep=sleep,
    )

    gate.defer(75)
    with gate.slot(PRIORITY_DASHBOARD):
        pass
    with gate.slot(PRIORITY_ALERT):
        pass

    # The deferral holds every start back, then normal spacing resumes.
    assert sleeps == [75, 1.0]


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_queued_alert_request_starts_before_older_dashboard_request():
    gate = RequestGate(max_concurrent=1, requests_per_second=1000)
    order = []
    lock = threading.Lock()

    def waiter(name, priority):
        def run():
            with gate.slot(priority):
                with lock:
                    order.append(name)

        return run

    with gate.slot(PRIORITY_DASHBOARD):
        # The dashboard request queues first, the alert request arrives later.
        dashboard = threading.Thread(target=waiter("dashboard", PRIORITY_DASHBOARD))
        dashboard.start()
        assert _wait_for(lambda: gate.pending_priorities == (PRIORITY_DASHBOARD,))
        alert = threading.Thread(target=waiter("alert", PRIORITY_ALERT))
        alert.start()
        assert _wait_for(lambda: gate.pending_priorities == (PRIORITY_ALERT, PRIORITY_DASHBOARD))

    alert.join(5.0)
    dashboard.join(5.0)

    assert order == ["alert", "dashboard"]


class TestFacilityIdentityCache:
    def test_store_and_get_roundtrip(self):
        cache = FacilityIdentityCache()
        facility = make_facility(232491)
        cache.store("232491", facility)
        assert cache.get("232491") is facility
        assert len(cache) == 1

    def test_get_missing_returns_none(self):
        cache = FacilityIdentityCache()
        assert cache.get("999") is None

    def test_entries_expire_after_ttl(self):
        now = [0.0]
        cache = FacilityIdentityCache(ttl_seconds=100, clock=lambda: now[0])
        cache.store("1", make_facility(1))
        now[0] = 99.0
        assert cache.get("1") is not None
        now[0] = 101.0
        assert cache.get("1") is None
        assert len(cache) == 0

    def test_eviction_keeps_most_recently_used(self):
        cache = FacilityIdentityCache(max_entries=2)
        cache.store("1", make_facility(1))
        cache.store("2", make_facility(2))
        cache.get("1")
        cache.store("3", make_facility(3))
        assert cache.get("1") is not None
        assert cache.get("2") is None
        assert cache.get("3") is not None

    def test_clear_empties_cache(self):
        cache = FacilityIdentityCache()
        cache.store("1", make_facility(1))
        cache.clear()
        assert len(cache) == 0


class TestIdentityCachedRecreationDotGov:
    @pytest.fixture
    def parent_lookup(self, monkeypatch):
        """Stub the camply RIDB lookup and record every call."""
        calls = []
        facilities = {
            "232491": make_facility(232491, name="Kirby Cove"),
            "232447": make_facility(232447, name="Upper Pines", area="Yosemite"),
        }

        def fake_lookup(self, campground_id):
            calls.append(list(campground_id))
            return [
                facilities[str(identifier)]
                for identifier in campground_id
                if str(identifier) in facilities
            ]

        monkeypatch.setattr(
            RecreationDotGov,
            "_find_facilities_from_campgrounds",
            fake_lookup,
        )
        return calls, facilities

    def test_first_lookup_fetches_and_caches(self, parent_lookup):
        calls, facilities = parent_lookup
        provider = IdentityCachedRecreationDotGov()
        result = provider._find_facilities_from_campgrounds([232491])
        assert result == [facilities["232491"]]
        assert calls == [[232491]]
        assert FACILITY_IDENTITY_CACHE.get("232491") is facilities["232491"]

    def test_second_lookup_skips_network(self, parent_lookup):
        calls, facilities = parent_lookup
        first = IdentityCachedRecreationDotGov()
        second = IdentityCachedRecreationDotGov()
        first._find_facilities_from_campgrounds([232491])
        result = second._find_facilities_from_campgrounds([232491])
        assert result == [facilities["232491"]]
        assert calls == [[232491]]

    def test_partial_hit_fetches_only_misses_and_preserves_order(self, parent_lookup):
        calls, facilities = parent_lookup
        provider = IdentityCachedRecreationDotGov()
        FACILITY_IDENTITY_CACHE.store("232447", facilities["232447"])
        result = provider._find_facilities_from_campgrounds([232491, 232447])
        assert calls == [[232491]]
        assert result == [facilities["232491"], facilities["232447"]]

    def test_unresolvable_ids_are_omitted_and_not_cached(self, parent_lookup):
        calls, _facilities = parent_lookup
        provider = IdentityCachedRecreationDotGov()
        result = provider._find_facilities_from_campgrounds([999])
        assert result == []
        assert FACILITY_IDENTITY_CACHE.get("999") is None
        provider._find_facilities_from_campgrounds([999])
        assert calls == [[999], [999]]

    def test_lookup_failure_propagates_and_caches_nothing(self, monkeypatch):
        def failing_lookup(self, campground_id):
            raise ConnectionError("RIDB unavailable")

        monkeypatch.setattr(
            RecreationDotGov,
            "_find_facilities_from_campgrounds",
            failing_lookup,
        )
        provider = IdentityCachedRecreationDotGov()
        with pytest.raises(ConnectionError):
            provider._find_facilities_from_campgrounds([232491])
        assert len(FACILITY_IDENTITY_CACHE) == 0

    def test_searcher_construction_reuses_identity(self, parent_lookup):
        calls, facilities = parent_lookup
        start = date.today() + timedelta(days=30)
        window = SearchWindow(start_date=start, end_date=start + timedelta(days=2))
        first = NativeSearchRecreationDotGov(search_window=window, campgrounds=[232491], nights=1)
        second = NativeSearchRecreationDotGov(search_window=window, campgrounds=[232491], nights=1)
        assert calls == [[232491]]
        assert first.campgrounds == [facilities["232491"]]
        assert second.campgrounds == first.campgrounds
