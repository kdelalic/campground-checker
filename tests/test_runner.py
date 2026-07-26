"""Tests for polling, persistence, and garbage-collection helpers."""

import threading
from datetime import date, timedelta
from types import SimpleNamespace

from campsite_checker.runner import (
    _advance_poll_deadline,
    _load_sent_keys,
    _maybe_collect_garbage,
    _save_sent_keys,
    run_forever,
)


def test_sent_keys_are_only_written_when_content_changes(tmp_path):
    path = tmp_path / "sent.json"
    keys = {("Camp", 1, date.today() + timedelta(days=1))}

    assert _save_sent_keys(path, keys) is True
    first_content = path.read_text()
    assert _save_sent_keys(path, keys) is False
    assert path.read_text() == first_content
    assert _load_sent_keys(path) == keys


def test_garbage_collection_runs_at_configured_interval(monkeypatch):
    collect_calls = []
    times = iter([10.0, 10.25])
    memory = iter([100.0, 90.0])
    monkeypatch.setattr("campsite_checker.runner.gc.collect", lambda: collect_calls.append(1) or 7)
    monkeypatch.setattr("campsite_checker.runner.monotonic", lambda: next(times))
    monkeypatch.setattr(
        "campsite_checker.runner._resident_memory_mb",
        lambda: next(memory),
    )

    assert _maybe_collect_garbage(scan_num=12, interval=12) is True
    assert collect_calls == [1]


def test_garbage_collection_can_be_skipped_or_disabled(monkeypatch):
    monkeypatch.setattr(
        "campsite_checker.runner.gc.collect",
        lambda: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    assert _maybe_collect_garbage(scan_num=11, interval=12) is False
    assert _maybe_collect_garbage(scan_num=12, interval=0) is False


def test_poll_deadline_stays_anchored_and_skips_overruns():
    assert _advance_poll_deadline(100.0, 60.0, now=120.0) == 160.0
    assert _advance_poll_deadline(100.0, 60.0, now=161.0) == 220.0
    assert _advance_poll_deadline(100.0, 60.0, now=281.0) == 340.0


def test_alerts_continue_while_dashboard_scan_runs(monkeypatch, tmp_path):
    events = []
    sent_keys_path = tmp_path / "sent.json"
    alert_key = ("Alert Camp", 1, date.today() + timedelta(days=1))
    dashboard_started = threading.Event()
    release_dashboard = threading.Event()
    dashboard_finished = threading.Event()
    sleep_calls = 0

    def fake_run_once(entries, *args, scan_type=None, **kwargs):
        if scan_type == "alert":
            events.append("alert")
            return {alert_key}, ["available"], []
        events.append("dashboard-start")
        dashboard_started.set()
        assert release_dashboard.wait(timeout=2)
        events.append("dashboard-end")
        dashboard_finished.set()
        return set(), [], []

    def fake_send_notifications(found, token, chat_id, prev_keys):
        events.append("notify")
        assert found == ["available"]
        return 1

    def fake_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            assert dashboard_started.wait(timeout=2)
            return
        release_dashboard.set()
        raise KeyboardInterrupt

    monkeypatch.setattr("campsite_checker.runner.run_once", fake_run_once)
    monkeypatch.setattr(
        "campsite_checker.runner._send_notifications",
        fake_send_notifications,
    )
    monkeypatch.setattr("campsite_checker.runner.time.sleep", fake_sleep)
    monkeypatch.setattr("campsite_checker.runner.SENT_KEYS_FILE", sent_keys_path)
    monkeypatch.setattr(
        "campsite_checker.server.start_healthcheck_server",
        lambda: None,
    )

    run_forever(
        entries=[
            {"provider": "RecreationDotGov", "campground_id": 1, "alert": True},
            {"provider": "ReserveCalifornia", "campground_id": 2, "alert": False},
        ],
        raw_config={},
        config_path="campsites.yaml",
        args=SimpleNamespace(
            alert_interval=1,
            dashboard_interval=10,
            gc_interval=0,
        ),
        day_filter=None,
        tg_token=None,
        tg_chat_id=None,
    )

    assert dashboard_finished.wait(timeout=2)
    assert events == [
        "alert",
        "notify",
        "dashboard-start",
        "alert",
        "notify",
        "dashboard-end",
    ]
    assert _load_sent_keys(sent_keys_path) == {alert_key}
