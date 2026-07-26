"""Tests for polling, persistence, and garbage-collection helpers."""

from datetime import date, timedelta

from campsite_checker.runner import (
    _advance_poll_deadline,
    _load_sent_keys,
    _maybe_collect_garbage,
    _save_sent_keys,
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
