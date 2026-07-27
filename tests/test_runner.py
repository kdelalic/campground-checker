"""Tests for polling, persistence, and garbage-collection helpers."""

import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace

from campsite_checker.runner import (
    _advance_poll_deadline,
    _load_sent_keys,
    _maybe_collect_garbage,
    _save_sent_keys,
    run_forever,
)


def _key(cid="1", campsite=1, days_ahead=1):
    return ("RecreationDotGov", cid, campsite, date.today() + timedelta(days=days_ahead))


def test_sent_keys_are_only_written_when_content_changes(tmp_path):
    path = tmp_path / "sent.json"
    keys = {_key()}

    assert _save_sent_keys(path, keys) is True
    first_content = path.read_text()
    assert _save_sent_keys(path, keys) is False
    assert path.read_text() == first_content
    assert _load_sent_keys(path) == keys


def test_sent_keys_keep_far_future_dates(tmp_path):
    """Keys must survive for the whole ~6-month search window, not 14 days,
    so a redeploy cannot re-alert far-out bookings."""
    path = tmp_path / "sent.json"
    keys = {_key(days_ahead=170)}

    assert _save_sent_keys(path, keys) is True
    assert _load_sent_keys(path) == keys


def test_sent_keys_prune_past_dates(tmp_path):
    path = tmp_path / "sent.json"
    past = _key(campsite=1, days_ahead=-1)
    future = _key(campsite=2, days_ahead=1)

    _save_sent_keys(path, {past, future})
    assert _load_sent_keys(path) == {future}


def test_corrupt_sent_keys_file_returns_empty_and_warns(tmp_path, caplog):
    path = tmp_path / "sent.json"
    path.write_text('[["RecreationDotGov","1",1,"2026-')  # truncated write

    with caplog.at_level("WARNING"):
        assert _load_sent_keys(path) == set()
    assert "unreadable sent-key state" in caplog.text


def test_sent_keys_write_is_atomic(tmp_path):
    """The write goes through a temp file + rename, so no partial file is left."""
    path = tmp_path / "sent.json"
    _save_sent_keys(path, {_key()})
    assert not path.with_name(path.name + ".tmp").exists()


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


def _forever_args():
    return SimpleNamespace(
        alert_interval=1,
        dashboard_interval=10,
        gc_interval=0,
    )


def _forever_entries():
    return [
        {"provider": "RecreationDotGov", "campground_id": 1, "alert": True},
        {"provider": "ReserveCalifornia", "campground_id": 2, "alert": False},
    ]


def test_alerts_continue_while_dashboard_scan_runs(monkeypatch, tmp_path):
    from campsite_checker.server import scan_status

    # GitHub runners can have less uptime than the dashboard interval. Keep the
    # monotonic clock near zero to verify the initial scan is still immediate.
    monotonic_origin = time.monotonic()
    events = []
    sent_keys_path = tmp_path / "sent.json"
    alert_key = _key()
    dashboard_started = threading.Event()
    release_dashboard = threading.Event()
    dashboard_finished = threading.Event()
    sleep_calls = 0
    initial_dashboard_scans = scan_status.dashboard_scan_count

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
        return 1, 0

    def fake_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            assert dashboard_started.wait(timeout=2)
            return
        if sleep_calls == 2:
            release_dashboard.set()
            assert dashboard_finished.wait(timeout=2)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("campsite_checker.runner.run_once", fake_run_once)
    monkeypatch.setattr(
        "campsite_checker.runner.monotonic",
        lambda: time.monotonic() - monotonic_origin,
    )
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
        entries=_forever_entries(),
        raw_config={},
        config_path="campsites.yaml",
        args=_forever_args(),
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
        "alert",
        "notify",
    ]
    assert _load_sent_keys(sent_keys_path) == {alert_key}
    assert scan_status.dashboard_scan_in_progress is False
    assert scan_status.dashboard_scan_count == initial_dashboard_scans + 1


def test_failed_telegram_send_defers_sent_key_checkpoint(monkeypatch, tmp_path):
    """Keys must not be persisted as sent while any message failed, so the
    alert is retried on the next scan instead of being lost forever."""
    sent_keys_path = tmp_path / "sent.json"
    alert_key = _key()
    outcomes = iter([(0, 1), (1, 0)])  # first send fails, second succeeds
    scans = []

    def fake_run_once(entries, *args, scan_type=None, **kwargs):
        scans.append(scan_type)
        return {alert_key}, ["available"], []

    def fake_send_notifications(found, token, chat_id, prev_keys):
        # New availability is visible on both scans: the failed first send
        # must not have marked it as already alerted.
        assert alert_key not in prev_keys
        return next(outcomes)

    def fake_sleep(seconds):
        if len(scans) >= 2:
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
        entries=[{"provider": "RecreationDotGov", "campground_id": 1, "alert": True}],
        raw_config={},
        config_path="campsites.yaml",
        args=_forever_args(),
        day_filter=None,
        tg_token=None,
        tg_chat_id=None,
    )

    # Nothing persisted after the failed scan; persisted after the success.
    assert _load_sent_keys(sent_keys_path) == {alert_key}
    assert len(scans) == 2
