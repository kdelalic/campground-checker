"""Tests for the ReserveCalifornia (UseDirect) provider hardening."""

import inspect
import pathlib
from contextlib import contextmanager
from types import SimpleNamespace

import camply
import pytest
import requests

from campsite_checker.providers.reserve_california import (
    CAMPLY_CACHE_DIR_ENV,
    DEFAULT_CAMPLY_CACHE_DIR,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    RESERVE_CALIFORNIA_REQUEST_GATE,
    TimeoutReserveCalifornia,
    TimeoutSearchReserveCalifornia,
    provider_class_for_priority,
)

PRIORITY_ALERT = 0
PRIORITY_DASHBOARD = 1


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


class TestProviderClassForPriority:
    def test_alert_priority_reuses_the_base_class(self):
        assert provider_class_for_priority(PRIORITY_ALERT) is TimeoutReserveCalifornia

    def test_dashboard_priority_carries_priority_on_the_class(self):
        provider_class = provider_class_for_priority(PRIORITY_DASHBOARD)

        # Camply builds the provider with a bare ``provider_class()`` call, so
        # the priority has to travel on the class itself.
        assert provider_class.request_priority == PRIORITY_DASHBOARD
        assert issubclass(provider_class, TimeoutReserveCalifornia)
        assert provider_class.request_gate is RESERVE_CALIFORNIA_REQUEST_GATE

    def test_classes_are_cached_per_priority(self):
        assert provider_class_for_priority(PRIORITY_DASHBOARD) is provider_class_for_priority(
            PRIORITY_DASHBOARD
        )


class TestTimeoutSearchReserveCalifornia:
    def test_recreation_area_stays_a_required_parameter(self):
        """``search._requires_recreation_area`` relies on this to pass ``[]``."""
        parameters = inspect.signature(TimeoutSearchReserveCalifornia.__init__).parameters

        assert "recreation_area" in parameters
        assert parameters["recreation_area"].default is inspect.Parameter.empty

    def test_declares_request_priority(self):
        parameters = inspect.signature(TimeoutSearchReserveCalifornia.__init__).parameters

        assert parameters["request_priority"].default == PRIORITY_ALERT

    def test_priority_binds_the_provider_class_before_camply_builds_it(self, monkeypatch):
        """The provider must already carry the priority when camply builds it."""
        observed = {}

        def fake_init(self, *args, **kwargs):
            observed["provider_priority"] = self.provider_class.request_priority

        monkeypatch.setattr(
            TimeoutSearchReserveCalifornia.__mro__[1],
            "__init__",
            fake_init,
        )
        search = TimeoutSearchReserveCalifornia(
            search_window=object(),
            recreation_area=[],
            request_priority=PRIORITY_DASHBOARD,
        )

        assert observed["provider_priority"] == PRIORITY_DASHBOARD
        assert search.request_priority == PRIORITY_DASHBOARD

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
