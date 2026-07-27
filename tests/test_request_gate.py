"""Tests for the shared priority-aware provider request gate."""

import threading
import time

import requests

from campsite_checker.request_gate import (
    PRIORITY_ALERT_REQUEST,
    RequestGate,
    pause_gate_on_rate_limit,
)

PRIORITY_ALERT = PRIORITY_ALERT_REQUEST
PRIORITY_DASHBOARD = 1


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FakeClock:
    def __init__(self, now=10.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_request_gate_spaces_provider_request_starts():
    clock = FakeClock()
    gate = RequestGate(
        max_concurrent=1,
        requests_per_second=2,
        clock=clock,
        sleep=clock.sleep,
    )

    with gate:
        pass
    with gate:
        pass

    assert clock.sleeps == [0.5]


def test_request_gate_without_a_rate_bounds_concurrency_only():
    clock = FakeClock()
    gate = RequestGate(max_concurrent=2, clock=clock, sleep=clock.sleep)

    for _ in range(3):
        with gate:
            pass

    # No spacing is imposed: the queue exists purely to order waiters.
    assert clock.sleeps == []


def test_request_gate_defer_pauses_all_future_starts():
    clock = FakeClock()
    gate = RequestGate(
        max_concurrent=2,
        requests_per_second=1,
        clock=clock,
        sleep=clock.sleep,
    )

    gate.defer(75)
    with gate.slot(PRIORITY_DASHBOARD):
        pass
    with gate.slot(PRIORITY_ALERT):
        pass

    # The deferral holds every start back, then normal spacing resumes.
    assert clock.sleeps == [75, 1.0]


def test_defer_applies_to_a_gate_with_no_configured_rate():
    clock = FakeClock()
    gate = RequestGate(max_concurrent=2, clock=clock, sleep=clock.sleep)

    gate.defer(30)
    with gate:
        pass

    assert clock.sleeps == [30]


def test_queued_alert_request_starts_before_older_dashboard_request():
    gate = RequestGate(max_concurrent=1)
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


class TestReservedAlertSlot:
    """A slot is held back so alerts never wait on in-flight dashboard work."""

    def test_alert_starts_while_dashboard_requests_saturate_their_cap(self):
        # Reproduces the production regression: ordering the queue was not
        # enough, because every alert request still had to wait for one of the
        # in-flight dashboard requests to finish.
        gate = RequestGate(max_concurrent=3)
        assert gate.deprioritized_slots == 2
        release = threading.Event()
        holders_started = threading.Semaphore(0)
        alert_started = threading.Event()

        def hold_dashboard():
            with gate.slot(PRIORITY_DASHBOARD):
                holders_started.release()
                assert release.wait(5.0)

        # Enough dashboard requests to fill every slot if none were reserved.
        holders = [threading.Thread(target=hold_dashboard) for _ in range(3)]
        for holder in holders:
            holder.start()
        for _ in range(gate.deprioritized_slots):
            assert holders_started.acquire(timeout=5.0)
        assert _wait_for(lambda: gate.pending_priorities == (PRIORITY_DASHBOARD,))

        def run_alert():
            with gate.slot(PRIORITY_ALERT):
                alert_started.set()

        alert = threading.Thread(target=run_alert)
        alert.start()

        # The alert runs to completion without any dashboard request finishing.
        assert alert_started.wait(5.0)
        assert not release.is_set()

        release.set()
        alert.join(5.0)
        for holder in holders:
            holder.join(5.0)

    def test_dashboard_requests_cannot_take_the_reserved_slot(self):
        gate = RequestGate(max_concurrent=2)
        assert gate.deprioritized_slots == 1
        release = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()

        def dashboard(started):
            def run():
                with gate.slot(PRIORITY_DASHBOARD):
                    started.set()
                    assert release.wait(5.0)

            return run

        first = threading.Thread(target=dashboard(first_started))
        first.start()
        assert first_started.wait(5.0)
        second = threading.Thread(target=dashboard(second_started))
        second.start()
        assert _wait_for(lambda: gate.pending_priorities == (PRIORITY_DASHBOARD,))

        # The free slot stays reserved rather than going to dashboard work.
        assert not second_started.is_set()

        release.set()
        assert second_started.wait(5.0)
        first.join(5.0)
        second.join(5.0)

    def test_single_slot_gate_is_shared(self):
        gate = RequestGate(max_concurrent=1)

        # With one slot there is nothing to reserve; it is shared instead.
        assert gate.deprioritized_slots == 1
        with gate.slot(PRIORITY_DASHBOARD):
            pass
        with gate.slot(PRIORITY_ALERT):
            pass

    def test_released_dashboard_slots_are_returned_to_the_pool(self):
        gate = RequestGate(max_concurrent=2)

        for _ in range(3):
            with gate.slot(PRIORITY_DASHBOARD):
                pass

        # A leaked counter would wedge dashboard work after the first request.
        assert gate.pending_priorities == ()
        assert gate._active_deprioritized == 0


def test_concurrency_cap_is_enforced():
    gate = RequestGate(max_concurrent=2)
    peak = 0
    current = 0
    lock = threading.Lock()
    release = threading.Event()

    def run():
        nonlocal peak, current
        with gate.slot(PRIORITY_ALERT):
            with lock:
                current += 1
                peak = max(peak, current)
            assert release.wait(5.0)
            with lock:
                current -= 1

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert _wait_for(lambda: peak == 2)
    release.set()
    for thread in threads:
        thread.join(5.0)

    assert peak == 2


def test_abandoned_waiter_does_not_block_the_queue():
    gate = RequestGate(max_concurrent=1)

    def boom(_seconds):
        raise KeyboardInterrupt("interrupted while waiting")

    gate._sleep = boom
    gate.defer(60)

    try:
        with gate.slot(PRIORITY_ALERT):
            pass
    except KeyboardInterrupt:
        pass

    # The abandoned ticket is removed, so the queue is not left wedged.
    assert gate.pending_priorities == ()


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code} response", response=self)


def _rate_limit_error(status_code=429, headers=None):
    try:
        FakeResponse(status_code, headers).raise_for_status()
    except requests.HTTPError as exc:
        return exc
    raise AssertionError("expected an HTTPError")


class TestPauseGateOnRateLimit:
    def test_retry_after_is_honored(self):
        clock = FakeClock()
        gate = RequestGate(max_concurrent=1, clock=clock, sleep=clock.sleep)

        pause_gate_on_rate_limit(
            gate,
            _rate_limit_error(headers={"Retry-After": "75"}),
            provider="TestProvider",
        )
        with gate:
            pass

        assert clock.sleeps == [75]

    def test_missing_retry_after_falls_back_to_default_pause(self):
        clock = FakeClock()
        gate = RequestGate(max_concurrent=1, clock=clock, sleep=clock.sleep)

        pause_gate_on_rate_limit(gate, _rate_limit_error(), provider="TestProvider")
        with gate:
            pass

        assert clock.sleeps == [30]

    def test_non_rate_limit_errors_do_not_pause(self):
        clock = FakeClock()
        gate = RequestGate(max_concurrent=1, clock=clock, sleep=clock.sleep)

        pause_gate_on_rate_limit(
            gate,
            _rate_limit_error(status_code=500),
            provider="TestProvider",
        )
        with gate:
            pass

        assert clock.sleeps == []
