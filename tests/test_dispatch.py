"""Deterministic tests for process-wide search dispatch coordination."""

import threading
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from camply.containers import SearchWindow

from campsite_checker.dispatch import (
    PRIORITY_ALERT,
    PRIORITY_DASHBOARD,
    ProviderCooldownActive,
    SearchDispatcher,
    get_dispatcher,
    shutdown_dispatcher,
)
from campsite_checker.search import SearchOutcome, execute_searches
from campsite_checker.throttle import ProviderThrottleRegistry

TIMEOUT = 5.0


class FakeClock:
    """Controlled monotonic clock that wakes the dispatcher when advanced."""

    def __init__(self):
        self.now = 0.0
        self.dispatcher = None

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        if self.dispatcher is not None:
            self.dispatcher.kick()


class Task:
    """A dispatchable unit that records its start and blocks until released."""

    def __init__(self, name, order=None, lock=None, blocking=True):
        self.name = name
        self.started = threading.Event()
        self.release = threading.Event()
        self.order = order
        self.lock = lock or threading.Lock()
        if not blocking:
            self.release.set()

    def __call__(self):
        if self.order is not None:
            with self.lock:
                self.order.append(self.name)
        self.started.set()
        assert self.release.wait(TIMEOUT), f"task {self.name} never released"
        return self.name


class ConcurrencyProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def wrap(self, task):
        def run():
            with self.lock:
                self.current += 1
                self.peak = max(self.peak, self.current)
            try:
                return task()
            finally:
                with self.lock:
                    self.current -= 1

        return run


@pytest.fixture
def registry():
    return ProviderThrottleRegistry()


@pytest.fixture
def dispatchers():
    created = []

    def factory(**kwargs):
        dispatcher = SearchDispatcher(**kwargs)
        clock = kwargs.get("clock")
        if isinstance(clock, FakeClock):
            clock.dispatcher = dispatcher
        created.append(dispatcher)
        return dispatcher

    yield factory
    for dispatcher in created:
        dispatcher.shutdown(wait=False)


@pytest.fixture(autouse=True)
def _reset_global_dispatcher():
    yield
    shutdown_dispatcher(wait=False)


class TestAggregateWorkerLimit:
    def test_overlapping_alert_and_dashboard_work_shares_worker_limit(self, dispatchers, registry):
        dispatcher = dispatchers(workers=2, throttles=registry)
        probe = ConcurrencyProbe()
        order = []
        lock = threading.Lock()
        tasks = [Task(f"a{i}", order, lock) for i in range(3)]
        tasks += [Task(f"d{i}", order, lock) for i in range(2)]

        futures = []
        for task in tasks[:3]:
            futures.append(
                dispatcher.submit(probe.wrap(task), provider="X", priority=PRIORITY_ALERT)
            )
        for task in tasks[3:]:
            futures.append(
                dispatcher.submit(probe.wrap(task), provider="Y", priority=PRIORITY_DASHBOARD)
            )

        assert tasks[0].started.wait(TIMEOUT)
        assert tasks[1].started.wait(TIMEOUT)
        for task in tasks:
            task.release.set()
        results = [future.result(timeout=TIMEOUT) for future in futures]

        assert sorted(results) == ["a0", "a1", "a2", "d0", "d1"]
        assert probe.peak == 2


class TestPriority:
    def test_alert_work_selected_ahead_of_queued_dashboard_work(self, dispatchers, registry):
        dispatcher = dispatchers(workers=1, throttles=registry)
        order = []
        lock = threading.Lock()
        blocker = Task("blocker", order, lock)
        dash = Task("dash", order, lock, blocking=False)
        alert = Task("alert", order, lock, blocking=False)

        blocker_future = dispatcher.submit(blocker, provider="X", priority=PRIORITY_ALERT)
        assert blocker.started.wait(TIMEOUT)
        # Dashboard work is queued first, alert work arrives afterwards.
        dash_future = dispatcher.submit(dash, provider="X", priority=PRIORITY_DASHBOARD)
        alert_future = dispatcher.submit(alert, provider="X", priority=PRIORITY_ALERT)
        blocker.release.set()

        assert blocker_future.result(timeout=TIMEOUT) == "blocker"
        assert alert_future.result(timeout=TIMEOUT) == "alert"
        assert dash_future.result(timeout=TIMEOUT) == "dash"
        assert order == ["blocker", "alert", "dash"]

    def test_dashboard_work_cannot_consume_all_capacity(self, dispatchers, registry):
        dispatcher = dispatchers(workers=2, throttles=registry)
        assert dispatcher.dashboard_slots == 1
        dash1 = Task("dash1")
        dash2 = Task("dash2")
        alert = Task("alert", blocking=False)

        dispatcher.submit(dash1, provider="X", priority=PRIORITY_DASHBOARD)
        dash2_future = dispatcher.submit(dash2, provider="X", priority=PRIORITY_DASHBOARD)
        assert dash1.started.wait(TIMEOUT)

        # The reserved slot serves alert work even while dashboard work queues.
        alert_future = dispatcher.submit(alert, provider="X", priority=PRIORITY_ALERT)
        assert alert_future.result(timeout=TIMEOUT) == "alert"
        assert not dash2.started.is_set()

        dash1.release.set()
        assert dash2.started.wait(TIMEOUT)
        dash2.release.set()
        assert dash2_future.result(timeout=TIMEOUT) == "dash2"

    def test_aged_dashboard_work_never_overtakes_newer_alert_work(self, dispatchers, registry):
        clock = FakeClock()
        dispatcher = dispatchers(workers=2, throttles=registry, clock=clock)
        order = []
        lock = threading.Lock()
        blockers = [Task("b1", order, lock), Task("b2", order, lock)]
        dash = Task("dash", order, lock, blocking=False)
        alert = Task("alert", order, lock, blocking=False)

        for blocker in blockers:
            dispatcher.submit(blocker, provider="X", priority=PRIORITY_ALERT)
        assert blockers[0].started.wait(TIMEOUT)
        assert blockers[1].started.wait(TIMEOUT)
        dash_future = dispatcher.submit(dash, provider="X", priority=PRIORITY_DASHBOARD)
        alert_future = dispatcher.submit(alert, provider="X", priority=PRIORITY_ALERT)

        # However long the dashboard batch has waited, alert work still wins
        # the freed slot: alerts are never queued behind dashboard scans.
        clock.advance(3600)
        blockers[0].release.set()
        assert alert_future.result(timeout=TIMEOUT) == "alert"
        assert dash_future.result(timeout=TIMEOUT) == "dash"
        assert order.index("alert") < order.index("dash")
        blockers[1].release.set()


class TestWorkersOne:
    def test_single_slot_is_shared_and_alert_runs_next(self, dispatchers, registry):
        dispatcher = dispatchers(workers=1, throttles=registry)
        assert dispatcher.dashboard_slots == 1
        order = []
        lock = threading.Lock()
        dash1 = Task("dash1", order, lock)
        dash2 = Task("dash2", order, lock, blocking=False)
        alert = Task("alert", order, lock, blocking=False)

        dispatcher.submit(dash1, provider="X", priority=PRIORITY_DASHBOARD)
        # With workers=1 dashboard work may occupy the only slot...
        assert dash1.started.wait(TIMEOUT)
        dash2_future = dispatcher.submit(dash2, provider="X", priority=PRIORITY_DASHBOARD)
        alert_future = dispatcher.submit(alert, provider="X", priority=PRIORITY_ALERT)
        dash1.release.set()

        # ...but queued alert work runs as soon as the in-flight batch ends.
        assert alert_future.result(timeout=TIMEOUT) == "alert"
        assert dash2_future.result(timeout=TIMEOUT) == "dash2"
        assert order == ["dash1", "alert", "dash2"]


class TestProviderPacing:
    def test_search_delay_is_global_per_provider_across_scan_types(self, dispatchers, registry):
        clock = FakeClock()
        dispatcher = dispatchers(
            workers=4,
            search_delay=100,
            throttles=registry,
            clock=clock,
        )
        alert_x = Task("alert_x", blocking=False)
        dash_x = Task("dash_x", blocking=False)
        dash_y = Task("dash_y", blocking=False)

        alert_future = dispatcher.submit(alert_x, provider="X", priority=PRIORITY_ALERT)
        assert alert_future.result(timeout=TIMEOUT) == "alert_x"

        dash_x_future = dispatcher.submit(dash_x, provider="X", priority=PRIORITY_DASHBOARD)
        dash_y_future = dispatcher.submit(dash_y, provider="Y", priority=PRIORITY_DASHBOARD)

        # Provider Y is unpaced and runs immediately; provider X is still
        # inside the shared 100s pacing window opened by the alert scan.
        assert dash_y_future.result(timeout=TIMEOUT) == "dash_y"
        assert not dash_x_future.done()

        clock.advance(100)
        assert dash_x_future.result(timeout=TIMEOUT) == "dash_x"


class TestCooldownAtDispatch:
    def test_long_cooldown_fails_queued_work_from_both_scan_types(self, dispatchers):
        clock = FakeClock()
        registry = ProviderThrottleRegistry(clock=clock)
        dispatcher = dispatchers(
            workers=1,
            throttles=registry,
            clock=clock,
            max_cooldown_wait=30,  # below the 45s cooldown, so dashboard fails too
        )
        blocker = Task("blocker")
        queued_alert = Task("queued_alert", blocking=False)
        queued_dash = Task("queued_dash", blocking=False)
        other = Task("other", blocking=False)

        blocker_future = dispatcher.submit(blocker, provider="X", priority=PRIORITY_ALERT)
        assert blocker.started.wait(TIMEOUT)
        alert_future = dispatcher.submit(queued_alert, provider="X", priority=PRIORITY_ALERT)
        dash_future = dispatcher.submit(queued_dash, provider="X", priority=PRIORITY_DASHBOARD)
        other_future = dispatcher.submit(other, provider="Y", priority=PRIORITY_DASHBOARD)

        registry.record_rate_limit("X", retry_after_seconds=45)
        dispatcher.kick()

        with pytest.raises(ProviderCooldownActive) as alert_skip:
            alert_future.result(timeout=TIMEOUT)
        with pytest.raises(ProviderCooldownActive) as dash_skip:
            dash_future.result(timeout=TIMEOUT)
        assert alert_skip.value.cooldown_seconds == 45
        assert dash_skip.value.provider == "X"

        blocker.release.set()
        assert blocker_future.result(timeout=TIMEOUT) == "blocker"
        assert other_future.result(timeout=TIMEOUT) == "other"

    def test_short_cooldown_is_waited_out_by_dashboard_work(self, dispatchers):
        """A brief rate-limit pause must not cost a whole dashboard batch.

        Failing the batch instead would leave every campground in it stale
        until the next dashboard interval, far longer than the pause.
        """
        clock = FakeClock()
        registry = ProviderThrottleRegistry(clock=clock)
        dispatcher = dispatchers(
            workers=2,
            throttles=registry,
            clock=clock,
            max_cooldown_wait=60,
        )
        queued_dash = Task("queued_dash", blocking=False)

        registry.record_rate_limit("X", retry_after_seconds=30)
        dash_future = dispatcher.submit(queued_dash, provider="X", priority=PRIORITY_DASHBOARD)

        # Still inside the cooldown: queued, not failed.
        assert not queued_dash.started.wait(0.2)
        assert not dash_future.done()

        clock.advance(30)
        assert dash_future.result(timeout=TIMEOUT) == "queued_dash"

    def test_alert_work_never_waits_out_a_cooldown(self, dispatchers):
        """The alert scan re-runs within its interval, so it fails fast."""
        clock = FakeClock()
        registry = ProviderThrottleRegistry(clock=clock)
        dispatcher = dispatchers(
            workers=2,
            throttles=registry,
            clock=clock,
            max_cooldown_wait=600,
        )
        queued_alert = Task("queued_alert", blocking=False)

        registry.record_rate_limit("X", retry_after_seconds=30)
        alert_future = dispatcher.submit(queued_alert, provider="X", priority=PRIORITY_ALERT)

        with pytest.raises(ProviderCooldownActive) as skip:
            alert_future.result(timeout=TIMEOUT)
        assert skip.value.cooldown_seconds == 30
        assert not queued_alert.started.is_set()


class TestLifecycle:
    def test_task_exception_propagates_and_dispatcher_keeps_running(self, dispatchers, registry):
        dispatcher = dispatchers(workers=1, throttles=registry)

        def boom():
            raise ValueError("kaboom")

        failing = dispatcher.submit(boom, provider="X", priority=PRIORITY_ALERT)
        with pytest.raises(ValueError, match="kaboom"):
            failing.result(timeout=TIMEOUT)

        follow_up = dispatcher.submit(lambda: "ok", provider="X", priority=PRIORITY_ALERT)
        assert follow_up.result(timeout=TIMEOUT) == "ok"

    def test_shutdown_cancels_queued_work_and_stops_dispatch_thread(self, dispatchers, registry):
        dispatcher = dispatchers(workers=1, throttles=registry)
        blocker = Task("blocker")
        queued = Task("queued", blocking=False)

        blocker_future = dispatcher.submit(blocker, provider="X", priority=PRIORITY_ALERT)
        assert blocker.started.wait(TIMEOUT)
        queued_future = dispatcher.submit(queued, provider="X", priority=PRIORITY_ALERT)

        dispatcher.shutdown(wait=False)

        assert queued_future.cancelled()
        assert not queued.started.is_set()
        assert not dispatcher._thread.is_alive()
        with pytest.raises(RuntimeError):
            dispatcher.submit(lambda: None, provider="X")

        # The in-flight batch still completes normally.
        blocker.release.set()
        assert blocker_future.result(timeout=TIMEOUT) == "blocker"


def make_args(**overrides):
    values = {
        "nights": None,
        "weekends_only": False,
        "batch_size": 1,
        "workers": 2,
        "search_delay": 0.0,
        "verbose": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_window():
    start = date.today() + timedelta(days=1)
    return SearchWindow(start_date=start, end_date=start + timedelta(days=7))


class TestExecuteSearchesCoordination:
    def test_alert_scan_completes_while_dashboard_scan_is_blocked(self, monkeypatch, registry):
        monkeypatch.setattr("campsite_checker.search.PROVIDER_THROTTLES", registry)
        dashboard_started = threading.Event()
        release_dashboard = threading.Event()

        def fake_payload(entry, search_window, args, priority=PRIORITY_ALERT):
            if entry["provider"] == "ReserveCalifornia":
                dashboard_started.set()
                assert release_dashboard.wait(TIMEOUT)
            return SearchOutcome([], None, 0.01, {}, None)

        monkeypatch.setattr("campsite_checker.search._search_payload", fake_payload)
        args = make_args(workers=2)
        dashboard_entries = [
            {"provider": "ReserveCalifornia", "campground_id": campground_id}
            for campground_id in (1, 2)
        ]
        alert_entries = [{"provider": "RecreationDotGov", "campground_id": 3}]

        dashboard_results = {}

        def run_dashboard_scan():
            dashboard_results.update(
                execute_searches(
                    dashboard_entries,
                    make_window(),
                    args,
                    priority=PRIORITY_DASHBOARD,
                )
            )

        dashboard_thread = threading.Thread(target=run_dashboard_scan)
        dashboard_thread.start()
        try:
            assert dashboard_started.wait(TIMEOUT)
            # The alert scan runs to completion while the dashboard scan is
            # still blocked inside its first batch.
            alert_results = execute_searches(alert_entries, make_window(), args)
            assert alert_results[0][2] is None
            assert dashboard_thread.is_alive()
        finally:
            release_dashboard.set()
            dashboard_thread.join(TIMEOUT)

        assert not dashboard_thread.is_alive()
        assert dashboard_results[0][2] is None
        assert dashboard_results[1][2] is None

    def test_single_run_default_priority_reuses_global_dispatcher(self, monkeypatch, registry):
        monkeypatch.setattr("campsite_checker.search.PROVIDER_THROTTLES", registry)

        def fake_payload(entry, search_window, args, priority=PRIORITY_ALERT):
            return SearchOutcome([], None, 0.01, {}, None)

        monkeypatch.setattr("campsite_checker.search._search_payload", fake_payload)
        args = make_args()
        entries = [{"provider": "RecreationDotGov", "campground_id": 1}]

        first = execute_searches(entries, make_window(), args)
        dispatcher = get_dispatcher(args.workers, args.search_delay, registry)
        second = execute_searches(entries, make_window(), args)

        assert first[0][2] is None
        assert second[0][2] is None
        assert get_dispatcher(args.workers, args.search_delay, registry) is dispatcher

    def test_dispatcher_shutdown_cancellation_is_reported_as_error(self, monkeypatch, registry):
        monkeypatch.setattr("campsite_checker.search.PROVIDER_THROTTLES", registry)
        started = threading.Event()
        release = threading.Event()

        def fake_payload(entry, search_window, args, priority=PRIORITY_ALERT):
            started.set()
            assert release.wait(TIMEOUT)
            return SearchOutcome([], None, 0.01, {}, None)

        monkeypatch.setattr("campsite_checker.search._search_payload", fake_payload)
        args = make_args(workers=1)
        entries = [
            {"provider": "RecreationDotGov", "campground_id": campground_id}
            for campground_id in (1, 2)
        ]

        scan_results = {}
        scan_thread = threading.Thread(
            target=lambda: scan_results.update(execute_searches(entries, make_window(), args))
        )
        scan_thread.start()
        assert started.wait(TIMEOUT)
        shutdown_dispatcher(wait=False)
        release.set()
        scan_thread.join(TIMEOUT)

        assert not scan_thread.is_alive()
        assert scan_results[0][2] is None
        assert scan_results[1][2] == "[WARNING] Search cancelled during shutdown"
