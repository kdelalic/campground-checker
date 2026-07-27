"""Tests for campsite_checker.state (sent-key persistence)."""

from datetime import date, timedelta

from campsite_checker.state import load_sent_keys, save_sent_keys


def _key(cid="1", campsite=1, days_ahead=1):
    return ("RecreationDotGov", cid, campsite, date.today() + timedelta(days=days_ahead))


def test_sent_keys_are_only_written_when_content_changes(tmp_path):
    path = tmp_path / "sent.json"
    keys = {_key()}

    assert save_sent_keys(path, keys) is True
    first_content = path.read_text()
    assert save_sent_keys(path, keys) is False
    assert path.read_text() == first_content
    assert load_sent_keys(path) == keys


def test_sent_keys_keep_far_future_dates(tmp_path):
    """Keys must survive for the whole ~6-month search window, not 14 days,
    so a redeploy cannot re-alert far-out bookings."""
    path = tmp_path / "sent.json"
    keys = {_key(days_ahead=170)}

    assert save_sent_keys(path, keys) is True
    assert load_sent_keys(path) == keys


def test_sent_keys_prune_past_dates(tmp_path):
    path = tmp_path / "sent.json"
    past = _key(campsite=1, days_ahead=-1)
    future = _key(campsite=2, days_ahead=1)

    save_sent_keys(path, {past, future})
    assert load_sent_keys(path) == {future}


def test_corrupt_sent_keys_file_returns_empty_and_warns(tmp_path, caplog):
    path = tmp_path / "sent.json"
    path.write_text('[["RecreationDotGov","1",1,"2026-')  # truncated write

    with caplog.at_level("WARNING"):
        assert load_sent_keys(path) == set()
    assert "unreadable sent-key state" in caplog.text


def test_sent_keys_write_is_atomic(tmp_path):
    """The write goes through a temp file + rename, so no partial file is left."""
    path = tmp_path / "sent.json"
    save_sent_keys(path, {_key()})
    assert not path.with_name(path.name + ".tmp").exists()
