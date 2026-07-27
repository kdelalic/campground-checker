"""Contract and retry tests for the native Recreation.gov search."""

import json
from contextlib import nullcontext
from datetime import date
from pathlib import Path

import pytest
import requests
from camply.containers import CampgroundFacility, SearchWindow

from campsite_checker.recreation_gov import (
    DEFAULT_REQUEST_TIMEOUT,
    NativeSearchRecreationDotGov,
    ProviderRequestMetrics,
    RequestGate,
)
from campsite_checker.results import process_results
from campsite_checker.throttle import detect_rate_limit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "recreation_gov_availability.json"


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


def make_facility():
    return CampgroundFacility(
        facility_name="Fixture Campground",
        recreation_area="Fixture National Park",
        facility_id=232447,
        recreation_area_id=2991,
        map_id=None,
        coordinates=None,
    )


class FakeIdentityProvider:
    def find_campgrounds(self, **_kwargs):
        return [make_facility()]


@pytest.fixture
def availability_payload():
    return json.loads(FIXTURE_PATH.read_text())


def make_search(monkeypatch, *, session, nights=1, metrics=None, campsites=None):
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
        request_gate=nullcontext(),
        request_metrics=metrics or ProviderRequestMetrics(),
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


def test_429_fails_immediately_for_existing_provider_cooldown(monkeypatch):
    metrics = ProviderRequestMetrics()
    response = FakeResponse(status_code=429, headers={"Retry-After": "75"})
    search = make_search(
        monkeypatch,
        session=QueueSession(response),
        metrics=metrics,
    )

    with pytest.raises(requests.HTTPError) as exc_info:
        search._request_month(232447, date(2099, 8, 1))

    detection = detect_rate_limit(exc_info.value)
    assert detection.rate_limited is True
    assert detection.retry_after_seconds == 75
    snapshot = metrics.snapshot()[0]
    assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (1, 0, 1)


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
