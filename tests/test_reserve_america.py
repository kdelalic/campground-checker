"""Tests for the native ReserveAmerica public-page search."""

import json
from datetime import date, timedelta

import pytest
import requests
from camply.containers import SearchWindow

from campsite_checker.providers.reserve_america import (
    NativeSearchReserveAmerica,
    ReserveAmericaResponseError,
    parse_availability_page,
)
from campsite_checker.request_gate import ProviderRequestMetrics, RequestGate


def availability_html(
    *,
    facility_id=110004,
    facility_name="Anthony Chabot",
    records=None,
    total_pages=1,
):
    if records is None:
        records = []
    state = {
        "backend": {
            "facility": {
                "facility": {
                    "id": facility_id,
                    "name": facility_name,
                    "coordinates": {
                        "latitude": 37.73528,
                        "longitude": -122.09611,
                    },
                }
            },
            "productSearch": {
                "searchResults": {
                    "records": records,
                    "totalPages": total_pages,
                }
            },
        }
    }
    return (
        '<html><script type="application/json" id="initialState">'
        f"{json.dumps(state)}"
        "</script></html>"
    )


def record(product_id, name, loop_name, statuses, *, product_group="Tent Site"):
    return {
        "id": product_id,
        "name": name,
        "prodGrpName": product_group,
        "prodInfo": {"typeOfUseLabel": "Overnight"},
        "details": {
            "loopName": loop_name,
            "attributes": [
                {"id": 111, "displayValue": ["1"]},
                {"id": 12, "displayValue": ["8"]},
                {"id": 11059, "displayValue": ["37.73"]},
                {"id": 11060, "displayValue": ["-122.09"]},
            ],
        },
        "availabilityGrid": [
            {"date": day.isoformat(), "status": status} for day, status in statuses.items()
        ],
    }


class FakeResponse:
    def __init__(self, text, status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} response",
                response=self,
            )


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_search(*, session=None, nights=1, campsites=None, sleep=lambda _seconds: None):
    start = date.today() + timedelta(days=1)
    return NativeSearchReserveAmerica(
        search_window=SearchWindow(
            start_date=start,
            end_date=start + timedelta(days=4),
        ),
        campgrounds=[110004],
        campsites=campsites,
        nights=nights,
        contract_code="eb",
        session=session,
        sleep=sleep,
        request_gate=RequestGate(max_concurrent=1),
        request_metrics=ProviderRequestMetrics(),
    )


class TestAvailabilityPageParsing:
    def test_extracts_facility_and_records(self):
        facility, results = parse_availability_page(
            availability_html(records=[{"id": 1}], total_pages=2)
        )

        assert facility["name"] == "Anthony Chabot"
        assert results["records"] == [{"id": 1}]
        assert results["totalPages"] == 2

    @pytest.mark.parametrize(
        "page",
        [
            "<html></html>",
            '<script type="application/json" id="initialState">{bad}</script>',
            ('<script type="application/json" id="initialState">{"backend": {}}</script>'),
        ],
    )
    def test_rejects_missing_or_invalid_state(self, page):
        with pytest.raises(ReserveAmericaResponseError):
            parse_availability_page(page)


class TestNativeSearchReserveAmerica:
    def test_requires_contract_and_campground(self):
        window = SearchWindow(
            start_date=date.today(),
            end_date=date.today() + timedelta(days=1),
        )
        with pytest.raises(ValueError, match="contract_code"):
            NativeSearchReserveAmerica(
                search_window=window,
                campgrounds=[1],
                contract_code="",
            )
        with pytest.raises(ValueError, match="campground ID"):
            NativeSearchReserveAmerica(
                search_window=window,
                campgrounds=[],
                contract_code="EB",
            )

    def test_requests_all_pages_with_public_query_parameters(self):
        session = FakeSession(
            [
                FakeResponse(availability_html(records=[{"id": 1}], total_pages=2)),
                FakeResponse(availability_html(records=[{"id": 2}], total_pages=2)),
            ]
        )
        search = make_search(session=session)
        start = date.today() + timedelta(days=1)

        facility, records = search._request_window(110004, start)

        assert facility["name"] == "Anthony Chabot"
        assert [item["id"] for item in records] == [1, 2]
        assert [call[1]["params"]["pageNumber"] for call in session.calls] == [0, 1]
        assert all(
            call[1]["params"]["availStartDate"] == start.isoformat() for call in session.calls
        )
        assert session.calls[0][0].endswith("/explore/campground/EB/110004/campsite-availability")

    def test_retries_timeout_then_succeeds(self):
        sleeps = []
        session = FakeSession(
            [
                requests.Timeout("slow"),
                FakeResponse(availability_html()),
            ]
        )
        search = make_search(session=session, sleep=sleeps.append)

        facility, results = search._request_page(
            110004,
            date.today() + timedelta(days=1),
            0,
        )

        assert facility["name"] == "Anthony Chabot"
        assert results["records"] == []
        assert sleeps == [1.0]
        snapshot = search._request_metrics.snapshot()[0]
        assert snapshot.attempts == 2
        assert snapshot.retries == 1
        assert snapshot.failures == 0

    def test_non_retryable_http_error_is_counted(self):
        session = FakeSession([FakeResponse("forbidden", status_code=403)])
        search = make_search(session=session)

        with pytest.raises(requests.HTTPError):
            search._request_page(
                110004,
                date.today() + timedelta(days=1),
                0,
            )

        snapshot = search._request_metrics.snapshot()[0]
        assert snapshot.attempts == 1
        assert snapshot.retries == 0
        assert snapshot.failures == 1

    def test_finds_only_consecutive_available_nights(self, monkeypatch):
        first = date.today() + timedelta(days=1)
        search = make_search(nights=2)
        records = [
            record(
                1,
                "001",
                "Loop A",
                {
                    first: "AVAILABLE",
                    first + timedelta(days=1): "AVAILABLE",
                    first + timedelta(days=2): "RESERVED",
                },
            ),
            record(
                2,
                "002",
                "Loop A",
                {
                    first: "AVAILABLE",
                    first + timedelta(days=1): "RESERVED",
                },
            ),
        ]
        facility = {
            "id": 110004,
            "name": "Anthony Chabot",
            "coordinates": {"latitude": 37.73528, "longitude": -122.09611},
        }
        monkeypatch.setattr(
            search,
            "_request_window",
            lambda _facility_id, _start: (facility, records),
        )

        results = search.get_matching_campsites(log=False)

        assert [(item.campsite_id, item.booking_date.date()) for item in results] == [(1, first)]
        result = results[0]
        assert result.facility_name == "Anthony Chabot"
        assert result.campsite_loop_name == "Loop A"
        assert result.booking_nights == 2
        assert result.location.latitude == 37.73
        assert "/EB/110004/1/campsite-booking" in result.booking_url

    def test_specific_campsite_filter_is_applied(self, monkeypatch):
        first = date.today() + timedelta(days=1)
        search = make_search(campsites=[2])
        records = [
            record(1, "001", "Loop A", {first: "AVAILABLE"}),
            record(2, "002", "Loop A", {first: "AVAILABLE"}),
        ]
        facility = {"id": 110004, "name": "Anthony Chabot", "coordinates": {}}
        monkeypatch.setattr(
            search,
            "_request_window",
            lambda _facility_id, _start: (facility, records),
        )

        results = search.get_matching_campsites(log=False)

        assert {item.campsite_id for item in results} == {2}

    def test_non_available_statuses_are_ignored(self, monkeypatch):
        first = date.today() + timedelta(days=1)
        search = make_search()
        records = [
            record(1, "001", "Loop A", {first: "CALL_CENTER"}),
            record(2, "002", "Loop A", {first: "WALK_UP"}),
            record(3, "003", "Loop A", {first: "AVAILABLE_BEYOUND_STAY"}),
        ]
        facility = {"id": 110004, "name": "Anthony Chabot", "coordinates": {}}
        monkeypatch.setattr(
            search,
            "_request_window",
            lambda _facility_id, _start: (facility, records),
        )

        results = search.get_matching_campsites(log=False)

        assert {item.campsite_id for item in results} == {3}
