"""Contract, windowing, and retry tests for the native ReserveCalifornia search."""

import inspect
import json
import os
import pathlib
import time
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import camply
import pytest
import requests
from camply.containers import CampgroundFacility, SearchWindow

from campsite_checker.providers.reserve_california import (
    CAMPLY_CACHE_DIR_ENV,
    DEFAULT_CAMPLY_CACHE_DIR,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT,
    GRID_URL,
    MAX_GRID_WINDOW_DAYS,
    METADATA_MAX_AGE_SECONDS,
    RESERVE_CALIFORNIA_REQUEST_GATE,
    RESERVE_CALIFORNIA_RESPONSE_CACHE,
    NativeSearchReserveCalifornia,
    TimeoutReserveCalifornia,
    provider_class_for_priority,
)
from campsite_checker.request_gate import ProviderRequestMetrics, SingleFlightTTLCache
from campsite_checker.results import process_results
from campsite_checker.throttle import detect_rate_limit

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "reserve_california_grid.json"
PRIORITY_ALERT = 0
PRIORITY_DASHBOARD = 1


@pytest.fixture(autouse=True)
def clear_response_cache():
    RESERVE_CALIFORNIA_RESPONSE_CACHE.clear()
    yield
    RESERVE_CALIFORNIA_RESPONSE_CACHE.clear()


class ImmediateGate:
    """Records slot priorities and deferrals without any real waiting."""

    def __init__(self):
        self.priorities = []
        self.deferrals = []
        self.fail_when_deferred = []

    @contextmanager
    def slot(self, priority=PRIORITY_ALERT, *, fail_when_deferred=False):
        self.priorities.append(priority)
        self.fail_when_deferred.append(fail_when_deferred)
        yield self

    def defer(self, seconds):
        self.deferrals.append(seconds)


def make_provider_self(gate, *, status_code=200, headers=None, priority=PRIORITY_ALERT):
    """A minimal stand-in for the camply provider instance."""
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        response = SimpleNamespace(
            status_code=status_code,
            url=kwargs["url"],
            text="",
            headers=headers or {},
        )

        def raise_for_status():
            if status_code >= 400:
                raise requests.HTTPError(f"{status_code} response", response=response)

        response.raise_for_status = raise_for_status
        return response

    return (
        SimpleNamespace(
            session=SimpleNamespace(request=fake_request),
            FIVE_HUNDRED_STATUS_CODES=[500, 502, 503],
            request_gate=gate,
            request_priority=priority,
        ),
        captured,
    )


class TestTimeoutReserveCalifornia:
    """The camply provider still serves identity and metadata lookups."""

    def test_make_http_request_passes_timeout(self):
        """Stock camply UseDirect requests hang forever without a timeout."""
        fake_self, captured = make_provider_self(ImmediateGate())

        response = TimeoutReserveCalifornia.make_http_request(fake_self, "https://example.com/api")

        assert response.status_code == 200
        assert captured["timeout"] == DEFAULT_HTTP_TIMEOUT_SECONDS

    def test_requests_are_issued_through_the_gate_at_the_instance_priority(self):
        gate = ImmediateGate()
        fake_self, _captured = make_provider_self(gate, priority=PRIORITY_DASHBOARD)

        TimeoutReserveCalifornia.make_http_request(fake_self, "https://example.com/api")

        assert gate.priorities == [PRIORITY_DASHBOARD]
        assert gate.deferrals == []

    def test_429_defers_the_gate_before_the_slot_is_released(self):
        gate = ImmediateGate()
        fake_self, _captured = make_provider_self(
            gate,
            status_code=429,
            headers={"Retry-After": "75"},
        )

        with pytest.raises(requests.HTTPError):
            TimeoutReserveCalifornia.make_http_request(fake_self, "https://example.com/api")

        assert gate.deferrals == [75]

    def test_server_errors_raise_provider_error_without_deferring(self):
        gate = ImmediateGate()
        fake_self, _captured = make_provider_self(gate, status_code=503)

        with pytest.raises(Exception, match="HTTP Error"):
            TimeoutReserveCalifornia.make_http_request(fake_self, "https://example.com/api")

        assert gate.deferrals == []

    def test_default_priority_is_alert_and_gate_is_shared(self):
        assert TimeoutReserveCalifornia.request_priority == PRIORITY_ALERT
        assert TimeoutReserveCalifornia.request_gate is RESERVE_CALIFORNIA_REQUEST_GATE

    def test_offline_cache_dir_defaults_outside_install_tree(self, monkeypatch):
        """Camply defaults the UseDirect cache to site-packages, which the
        unprivileged container user cannot write; the override must land
        somewhere else."""
        monkeypatch.delenv(CAMPLY_CACHE_DIR_ENV, raising=False)
        cache_dir = TimeoutReserveCalifornia().offline_cache_dir
        assert cache_dir == DEFAULT_CAMPLY_CACHE_DIR / "reserve-california"
        camply_install_tree = pathlib.Path(camply.__file__).parent
        assert camply_install_tree not in cache_dir.resolve().parents

    def test_offline_cache_dir_honors_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(CAMPLY_CACHE_DIR_ENV, str(tmp_path / "camply-cache"))
        cache_dir = TimeoutReserveCalifornia().offline_cache_dir
        assert cache_dir == tmp_path / "camply-cache" / "reserve-california"


class TestMetadataStaleness:
    """Camply's own 1-day expiry never fires on the paths this project uses.

    `find_campgrounds` sets ``active_search = True`` before refreshing, and
    ``_fetch_metadata_from_disk`` skips the expiry check whenever that flag is
    set — so on a persistent cache volume the metadata was frozen at whatever
    the first run downloaded.
    """

    @pytest.fixture
    def cached_provider(self, monkeypatch, tmp_path):
        def build(age_seconds):
            monkeypatch.setenv(CAMPLY_CACHE_DIR_ENV, str(tmp_path))
            provider = TimeoutReserveCalifornia()
            provider.offline_cache_dir.mkdir(parents=True, exist_ok=True)
            for name in ("filters", "cityparks", "places", "facilities"):
                cached = provider.offline_cache_dir / f"{name}.json"
                cached.write_text("[]")
                mtime = time.time() - age_seconds
                os.utime(cached, (mtime, mtime))
            return provider

        return build

    def test_fresh_metadata_is_not_refetched(self, cached_provider, monkeypatch):
        provider = cached_provider(age_seconds=60)
        calls = []
        monkeypatch.setattr(
            TimeoutReserveCalifornia, "refresh_metadata", lambda self: calls.append(1)
        )

        provider.refresh_stale_metadata()

        assert calls == []

    def test_expired_metadata_is_refetched_with_the_search_flag_clear(
        self, cached_provider, monkeypatch
    ):
        provider = cached_provider(age_seconds=METADATA_MAX_AGE_SECONDS + 60)
        observed = {}

        def fake_refresh(self):
            observed["active_search"] = self.active_search
            observed["metadata_refreshed"] = self.metadata_refreshed

        monkeypatch.setattr(TimeoutReserveCalifornia, "refresh_metadata", fake_refresh)
        # A previous lookup on this instance would otherwise suppress the refetch.
        provider.metadata_refreshed = True
        provider.active_search = True

        provider.refresh_stale_metadata()

        assert observed == {"active_search": False, "metadata_refreshed": False}

    def test_find_campgrounds_checks_staleness_first(self, cached_provider, monkeypatch):
        provider = cached_provider(age_seconds=METADATA_MAX_AGE_SECONDS + 60)
        order = []
        monkeypatch.setattr(
            TimeoutReserveCalifornia,
            "refresh_stale_metadata",
            lambda self: order.append("refresh"),
        )
        monkeypatch.setattr(
            TimeoutReserveCalifornia.__mro__[1],
            "find_campgrounds",
            lambda self, **kwargs: order.append("find") or [],
        )

        provider.find_campgrounds(campground_id=[786])

        assert order == ["refresh", "find"]

    def test_a_missing_cache_is_not_treated_as_stale(self, monkeypatch, tmp_path):
        """An absent cache is camply's own cold-start path, not an expiry."""
        monkeypatch.setenv(CAMPLY_CACHE_DIR_ENV, str(tmp_path / "empty"))
        calls = []
        monkeypatch.setattr(
            TimeoutReserveCalifornia, "refresh_metadata", lambda self: calls.append(1)
        )

        TimeoutReserveCalifornia().refresh_stale_metadata()

        assert calls == []


class TestProviderClassForPriority:
    def test_alert_priority_reuses_the_base_class(self):
        assert provider_class_for_priority(PRIORITY_ALERT) is TimeoutReserveCalifornia

    def test_dashboard_priority_carries_priority_on_the_class(self):
        provider_class = provider_class_for_priority(PRIORITY_DASHBOARD)

        # The provider is built before any per-request argument exists, so the
        # priority has to travel on the class itself.
        assert provider_class.request_priority == PRIORITY_DASHBOARD
        assert issubclass(provider_class, TimeoutReserveCalifornia)
        assert provider_class.request_gate is RESERVE_CALIFORNIA_REQUEST_GATE

    def test_classes_are_cached_per_priority(self):
        assert provider_class_for_priority(PRIORITY_DASHBOARD) is provider_class_for_priority(
            PRIORITY_DASHBOARD
        )


def make_facility(facility_id=786):
    return CampgroundFacility(
        facility_name="Fixture Campground",
        recreation_area="Fixture State Park",
        facility_id=facility_id,
        recreation_area_id=641,
        map_id=None,
        coordinates=None,
    )


class FakeIdentityProvider:
    """Stands in for the camply provider's identity and metadata lookups."""

    usedirect_unit_categories = {1: "Camping", 2: "RV"}
    usedirect_unit_type_groups = {1: "Campsite", 3: "RV Site"}

    def find_campgrounds(self, **_kwargs):
        return [make_facility()]

    def get_booking_url(self, recreation_area_id, facility_id):
        return f"https://www.reservecalifornia.com/park/{recreation_area_id}/{facility_id}"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} response", response=self)

    def json(self):
        return self._payload


class QueueSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse(payload={})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def bodies(self):
        return [json.loads(kwargs["data"]) for _url, kwargs in self.calls]


@pytest.fixture
def grid_payload():
    return json.loads(FIXTURE_PATH.read_text())


def grid_with_free_dates(days):
    """A minimal grid response with one unit free on ``days``."""
    return {
        "Facility": {
            "FacilityId": 786,
            "Latitude": 38.95499,
            "Longitude": -120.08248,
            "Units": {
                "40906": {
                    "UnitId": 40906,
                    "Name": "Campsite #1",
                    "UnitCategoryId": 1,
                    "UnitTypeGroupId": 1,
                    "Slices": {
                        f"{day.isoformat()}T00:00:00": {
                            "Date": day.isoformat(),
                            "IsFree": True,
                        }
                        for day in days
                    },
                }
            },
        }
    }


def parse_request_date(value):
    """Parse the provider's ``MM-DD-YYYY`` wire format."""
    month, day, year = (int(part) for part in value.split("-"))
    return date(year, month, day)


def requested_days(body):
    first = parse_request_date(body["StartDate"])
    last = parse_request_date(body["EndDate"])
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


def make_search(
    *,
    session,
    window=None,
    nights=1,
    weekends_only=False,
    metrics=None,
    campsites=None,
    request_gate=None,
    response_cache=None,
    request_priority=PRIORITY_ALERT,
):
    return NativeSearchReserveCalifornia(
        search_window=window
        or SearchWindow(start_date=date(2099, 8, 5), end_date=date(2099, 8, 9)),
        campgrounds=[786],
        campsites=campsites,
        nights=nights,
        weekends_only=weekends_only,
        session=session,
        sleep=lambda _seconds: None,
        request_gate=request_gate or ImmediateGate(),
        request_metrics=metrics or ProviderRequestMetrics(),
        response_cache=(response_cache if response_cache is not None else SingleFlightTTLCache()),
        request_priority=request_priority,
        provider=FakeIdentityProvider(),
    )


class TestResultContract:
    def test_fixture_parsing_preserves_downstream_result_contract(self, grid_payload):
        search = make_search(
            session=QueueSession(FakeResponse(payload=grid_payload)),
            nights=2,
        )

        results = search.get_matching_campsites(log=False)

        assert len(results) == 1
        result = results[0]
        assert result.campsite_id == 40906
        assert result.booking_date.date() == date(2099, 8, 5)
        assert result.booking_end_date.date() == date(2099, 8, 7)
        assert result.booking_nights == 2
        assert result.campsite_site_name == "Campsite #1"
        assert result.campsite_type == "Camping"
        assert result.campsite_use_type == "Campsite"
        assert result.facility_id == 786
        assert result.facility_name == "Fixture Campground"
        assert result.recreation_area == "Fixture State Park"
        assert result.booking_url == "https://www.reservecalifornia.com/park/641/786"
        assert (result.location.latitude, result.location.longitude) == (38.95499, -120.08248)

        processed = process_results(
            {"provider": "ReserveCalifornia", "campground_id": 786},
            results,
            day_filter=None,
        )
        assert processed.total_sites == 1
        assert processed.facility_name == "Fixture State Park — Fixture Campground"

    def test_only_free_slices_become_results(self, grid_payload):
        search = make_search(session=QueueSession(FakeResponse(payload=grid_payload)))

        results = search.get_matching_campsites(log=False)

        # 08-07 is reserved for unit 40906 and 08-05 is blocked for 40907.
        assert sorted((r.campsite_id, r.booking_date.date()) for r in results) == [
            (40906, date(2099, 8, 5)),
            (40906, date(2099, 8, 6)),
            (40906, date(2099, 8, 8)),
            (40907, date(2099, 8, 6)),
        ]

    def test_campsite_filter_is_applied(self, grid_payload):
        search = make_search(
            session=QueueSession(FakeResponse(payload=grid_payload)),
            campsites=[40907],
        )

        results = search.get_matching_campsites(log=False)

        assert [(r.campsite_id, r.booking_date.date()) for r in results] == [
            (40907, date(2099, 8, 6))
        ]

    def test_empty_facility_payload_yields_no_results(self):
        search = make_search(session=QueueSession(FakeResponse(payload={})))

        assert search.get_matching_campsites(log=False) == []


class TestWindowSlicing:
    """The grid endpoint truncates every response to 21 days of slices.

    Camply asks for a calendar month and silently loses days 22..EOM, so this
    is the behaviour the native client exists to fix.
    """

    def test_request_windows_cover_the_whole_search_window(self):
        session = QueueSession()
        start = date(2099, 8, 1)
        search = make_search(
            session=session,
            window=SearchWindow(start_date=start, end_date=start + timedelta(days=60)),
        )

        search.get_matching_campsites(log=False)

        bodies = session.bodies
        assert [body["StartDate"] for body in bodies] == ["08-01-2099", "08-22-2099", "09-12-2099"]
        assert [body["EndDate"] for body in bodies] == ["08-21-2099", "09-11-2099", "10-02-2099"]

        # No day of the search window falls between two requested ranges — the
        # gap camply's calendar-month loop leaves after the 21st.
        requested = {day for body in bodies for day in requested_days(body)}
        assert {start + timedelta(days=offset) for offset in range(60)} <= requested

    def test_window_never_exceeds_the_server_side_cap(self):
        session = QueueSession()
        start = date(2099, 8, 1)
        search = make_search(
            session=session,
            window=SearchWindow(start_date=start, end_date=start + timedelta(days=90)),
        )

        search.get_matching_campsites(log=False)

        for body in session.bodies:
            assert len(requested_days(body)) == MAX_GRID_WINDOW_DAYS

    def test_a_short_window_issues_a_single_request(self):
        session = QueueSession()
        search = make_search(session=session)

        search.get_matching_campsites(log=False)

        assert len(session.calls) == 1
        assert session.calls[0][0] == GRID_URL

    def test_trailing_nights_are_covered_when_weekends_only(self):
        """A Saturday start with nights=3 needs the Sunday and Monday too."""
        session = QueueSession()
        search = make_search(
            session=session,
            window=SearchWindow(start_date=date(2099, 8, 28), end_date=date(2099, 8, 30)),
            weekends_only=True,
            nights=3,
        )

        search.get_matching_campsites(log=False)

        body = session.bodies[0]
        assert body["StartDate"] == "08-28-2099"
        # 08-28 is a Friday and 08-29 a Saturday; the last stay ends 08-31.
        assert body["EndDate"] == "09-17-2099"

    def test_slices_outside_the_search_window_are_ignored(self, grid_payload):
        """Windows can overhang the search window; results must not."""
        search = make_search(
            session=QueueSession(FakeResponse(payload=grid_payload)),
            window=SearchWindow(start_date=date(2099, 8, 5), end_date=date(2099, 8, 7)),
        )

        results = search.get_matching_campsites(log=False)

        assert {r.booking_date.date() for r in results} == {date(2099, 8, 5), date(2099, 8, 6)}

    def test_availability_is_joined_across_windows(self):
        """A stay spanning a window boundary must still be found."""
        session = QueueSession(
            FakeResponse(payload=grid_with_free_dates([date(2099, 8, 21)])),
            FakeResponse(payload=grid_with_free_dates([date(2099, 8, 22)])),
        )
        search = make_search(
            session=session,
            window=SearchWindow(start_date=date(2099, 8, 1), end_date=date(2099, 8, 31)),
            nights=2,
        )

        results = search.get_matching_campsites(log=False)

        assert len(session.calls) == 2
        assert [(r.campsite_id, r.booking_date.date()) for r in results] == [
            (40906, date(2099, 8, 21))
        ]

    def test_weekends_only_matches_friday_and_saturday(self):
        search = make_search(session=QueueSession(), weekends_only=True)

        assert {day.weekday() for day in search._search_days()} == {4, 5}


class TestRequestRetries:
    """Bounded retries, in place of camply's 100-minute tenacity window."""

    def test_retryable_server_errors_use_short_bounded_retry(self):
        metrics = ProviderRequestMetrics()
        sleeps = []
        session = QueueSession(
            FakeResponse(status_code=500),
            FakeResponse(status_code=502),
            FakeResponse(payload={"Facility": {}}),
        )
        search = make_search(session=session, metrics=metrics)
        search._sleep = sleeps.append

        payload = search._request_window(786, date(2099, 8, 1), date(2099, 8, 21))

        assert payload == {"Facility": {}}
        assert sleeps == [1.0, 2.0]
        assert len(session.calls) == 3
        snapshot = metrics.snapshot()[0]
        assert snapshot.provider == "ReserveCalifornia"
        assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (3, 2, 0)
        assert all(call[1]["timeout"] == DEFAULT_REQUEST_TIMEOUT for call in session.calls)

    def test_429_defers_gate_by_retry_after_then_retries_successfully(self):
        metrics = ProviderRequestMetrics()
        gate = ImmediateGate()
        sleeps = []
        search = make_search(
            session=QueueSession(
                FakeResponse(status_code=429, headers={"Retry-After": "75"}),
                FakeResponse(payload={"Facility": {}}),
            ),
            metrics=metrics,
            request_gate=gate,
            request_priority=PRIORITY_DASHBOARD,
        )
        search._sleep = sleeps.append

        assert search._request_window(786, date(2099, 8, 1), date(2099, 8, 21)) == {"Facility": {}}

        # The pause is applied while the slot is still held, so batches already
        # executing stop issuing requests too. Re-acquiring the slot is what
        # waits it out, so no local backoff is slept on top of it.
        assert gate.deferrals == [75]
        assert gate.priorities == [PRIORITY_DASHBOARD, PRIORITY_DASHBOARD]
        assert sleeps == []
        snapshot = metrics.snapshot()[0]
        assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (2, 1, 0)

    def test_alert_requests_do_not_wait_for_a_rate_limit_retry(self):
        """An alert scan re-runs within its interval, so it must return promptly."""
        metrics = ProviderRequestMetrics()
        gate = ImmediateGate()
        search = make_search(
            session=QueueSession(FakeResponse(status_code=429)),
            metrics=metrics,
            request_gate=gate,
            request_priority=PRIORITY_ALERT,
        )

        with pytest.raises(requests.HTTPError) as exc_info:
            search._request_window(786, date(2099, 8, 1), date(2099, 8, 21))

        assert detect_rate_limit(exc_info.value).rate_limited is True
        assert gate.deferrals == [30]
        assert gate.fail_when_deferred == [True]
        snapshot = metrics.snapshot()[0]
        assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (1, 0, 1)

    def test_equivalent_stay_criteria_share_one_grid_fetch(self):
        cache = SingleFlightTTLCache()
        first_session = QueueSession(FakeResponse(payload={"Facility": {}}))
        second_session = QueueSession()
        one_night = make_search(
            session=first_session,
            nights=1,
            response_cache=cache,
        )
        two_nights = make_search(
            session=second_session,
            nights=2,
            response_cache=cache,
        )
        start = date(2099, 8, 1)
        end = date(2099, 8, 21)

        assert one_night._request_window(786, start, end) == {"Facility": {}}
        assert two_nights._request_window(786, start, end) == {"Facility": {}}
        assert len(first_session.calls) == 1
        assert second_session.calls == []

    def test_alerts_do_not_reuse_dashboard_response_cache_entries(self):
        cache = SingleFlightTTLCache()
        alert_session = QueueSession(FakeResponse(payload={"source": "alert"}))
        dashboard_session = QueueSession(FakeResponse(payload={"source": "dashboard"}))
        alert = make_search(
            session=alert_session,
            response_cache=cache,
            request_priority=PRIORITY_ALERT,
        )
        dashboard = make_search(
            session=dashboard_session,
            response_cache=cache,
            request_priority=PRIORITY_DASHBOARD,
        )
        start = date(2099, 8, 1)
        end = date(2099, 8, 21)

        assert alert._request_window(786, start, end) == {"source": "alert"}
        assert dashboard._request_window(786, start, end) == {"source": "dashboard"}
        assert len(alert_session.calls) == len(dashboard_session.calls) == 1

    def test_dashboard_requests_retry_rate_limits_twice_before_failing(self):
        metrics = ProviderRequestMetrics()
        gate = ImmediateGate()
        search = make_search(
            session=QueueSession(
                FakeResponse(status_code=429),
                FakeResponse(status_code=429),
                FakeResponse(status_code=429),
            ),
            metrics=metrics,
            request_gate=gate,
            request_priority=PRIORITY_DASHBOARD,
        )

        with pytest.raises(requests.HTTPError):
            search._request_window(786, date(2099, 8, 1), date(2099, 8, 21))

        assert gate.deferrals == [30, 30, 30]
        snapshot = metrics.snapshot()[0]
        assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (3, 2, 1)

    def test_server_errors_do_not_defer_the_shared_gate(self):
        gate = ImmediateGate()
        search = make_search(
            session=QueueSession(
                FakeResponse(status_code=500),
                FakeResponse(payload={"Facility": {}}),
            ),
            request_gate=gate,
        )

        assert search._request_window(786, date(2099, 8, 1), date(2099, 8, 21)) == {"Facility": {}}
        assert gate.deferrals == []

    def test_client_errors_are_not_retried(self):
        metrics = ProviderRequestMetrics()
        session = QueueSession(FakeResponse(status_code=400))
        search = make_search(session=session, metrics=metrics)

        with pytest.raises(requests.HTTPError):
            search._request_window(786, date(2099, 8, 1), date(2099, 8, 21))

        assert len(session.calls) == 1
        snapshot = metrics.snapshot()[0]
        assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (1, 0, 1)

    def test_timeouts_stop_after_three_attempts(self):
        metrics = ProviderRequestMetrics()
        search = make_search(
            session=QueueSession(
                requests.Timeout("one"),
                requests.Timeout("two"),
                requests.Timeout("three"),
            ),
            metrics=metrics,
        )

        with pytest.raises(requests.Timeout, match="three"):
            search._request_window(786, date(2099, 8, 1), date(2099, 8, 21))

        snapshot = metrics.snapshot()[0]
        assert (snapshot.attempts, snapshot.retries, snapshot.failures) == (3, 2, 1)


class TestSearcherContract:
    def test_grid_url_matches_the_provider_endpoint(self):
        assert GRID_URL == (
            "https://california-rdr.prod.cali.rd12.recreation-management."
            "tylerapp.com/rdr/search/grid"
        )

    def test_declares_request_priority(self):
        parameters = inspect.signature(NativeSearchReserveCalifornia.__init__).parameters

        assert parameters["request_priority"].default == PRIORITY_ALERT

    def test_recreation_area_is_optional(self):
        """Unlike camply's searcher, ``search.build_searcher`` need not pass []."""
        parameters = inspect.signature(NativeSearchReserveCalifornia.__init__).parameters

        assert parameters["recreation_area"].default is None

    def test_missing_identifiers_raise_instead_of_exiting(self):
        """Camply's UseDirect search calls ``sys.exit`` from the worker thread."""
        with pytest.raises(ValueError, match="campground or recreation area"):
            NativeSearchReserveCalifornia(
                search_window=SearchWindow(
                    start_date=date(2099, 8, 5),
                    end_date=date(2099, 8, 9),
                ),
                provider=FakeIdentityProvider(),
            )

    def test_nights_must_be_positive(self):
        with pytest.raises(ValueError, match="nights"):
            make_search(session=QueueSession(), nights=0)

    def test_continuous_searches_are_rejected(self):
        search = make_search(session=QueueSession())

        with pytest.raises(ValueError, match="one-shot"):
            search.get_matching_campsites(log=False, continuous=True)

    def test_no_resolved_campgrounds_raises(self):
        class EmptyProvider(FakeIdentityProvider):
            def find_campgrounds(self, **_kwargs):
                return []

        search = NativeSearchReserveCalifornia(
            search_window=SearchWindow(start_date=date(2099, 8, 5), end_date=date(2099, 8, 9)),
            campgrounds=[786],
            session=QueueSession(),
            request_gate=ImmediateGate(),
            provider=EmptyProvider(),
        )

        with pytest.raises(RuntimeError, match="No campgrounds"):
            search.get_matching_campsites(log=False)

    def test_search_days_never_reach_into_the_past(self):
        """The grid endpoint clamps StartDate itself; don't spend a day on it."""
        today = date.today()
        search = make_search(
            session=QueueSession(),
            window=SearchWindow(
                start_date=today - timedelta(days=30), end_date=today + timedelta(days=3)
            ),
        )

        assert min(search._search_days()) == today
