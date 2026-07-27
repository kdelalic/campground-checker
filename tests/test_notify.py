"""Tests for campsite_checker.notify."""

import io
import urllib.error
from datetime import datetime

import pytest

from campsite_checker.notify import (
    _MAX_TG_LEN,
    build_processed_telegram_message,
    filter_new_availability,
    send_telegram,
)
from campsite_checker.results import make_notification_key, process_results

from .conftest import make_campsite

# ── build_processed_telegram_message ────────────────────────────────────────


class TestBuildTelegramMessage:
    def test_empty_input_returns_empty(self):
        assert build_processed_telegram_message([]) == []

    def test_no_results_returns_empty(self):
        # All entries have results filtered out (boat-in)
        results = [make_campsite(campsite_type="BOAT-IN")]
        processed = process_results({"name": "Test"}, results, None)
        assert build_processed_telegram_message([processed]) == []

    def test_single_campground(self):
        results = [make_campsite(booking_date=datetime(2026, 7, 4))]
        processed = process_results({"name": "Test Camp"}, results, None)
        msgs = build_processed_telegram_message([processed])
        assert len(msgs) == 1
        assert "<b>Campsite Availability Found!</b>" in msgs[0]
        assert "Test Area — Test Campground" in msgs[0]
        assert "1 open site(s)" in msgs[0]

    def test_multiple_campgrounds_in_one_message(self):
        processed = [
            process_results(
                {"name": "Camp A"},
                [make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4))],
                None,
            ),
            process_results(
                {"name": "Camp B"},
                [make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 11))],
                None,
            ),
        ]
        msgs = build_processed_telegram_message(processed)
        assert len(msgs) == 1
        assert "Test Area — Test Campground" in msgs[0]

    def test_message_splitting_at_4096(self):
        """When content exceeds 4096 chars, it should split into multiple messages."""
        processed = []
        for i in range(50):
            results = [
                make_campsite(
                    campsite_id=i * 100 + j,
                    facility_name=f"Campground Number {i}",
                    booking_date=datetime(2026, 7, j + 1),
                )
                for j in range(5)
            ]
            processed.append(process_results({"name": f"Camp {i}"}, results, None))
        msgs = build_processed_telegram_message(processed)
        assert len(msgs) >= 2
        for msg in msgs:
            assert len(msg) <= _MAX_TG_LEN

    def test_html_escaping(self):
        results = [
            make_campsite(
                facility_name="<script>alert('xss')</script>",
                recreation_area="",
                booking_url="javascript:void(0)",
            )
        ]
        processed = process_results({"name": "Test"}, results, None)
        msgs = build_processed_telegram_message([processed])
        assert "<script>" not in msgs[0]
        assert "&lt;script&gt;" in msgs[0]


# ── filter_new_availability ─────────────────────────────────────────────────


class TestFilterNewAvailability:
    def test_alert_disabled_returns_empty(self):
        processed = process_results({"alert": False}, [make_campsite()], None)
        assert not filter_new_availability(processed, set()).available

    def test_all_new_results(self):
        entry = {"alert": True, "campground_id": 100}
        results = [make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4))]
        processed = process_results(entry, results, None)
        new = filter_new_availability(processed, set())
        assert new.total_sites == 1

    def test_previously_sent_filtered(self):
        entry = {"alert": True, "campground_id": 100}
        results = [make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4))]
        processed = process_results(entry, results, None)
        prev = set(processed.notification_keys)
        new = filter_new_availability(processed, prev)
        assert not new.available

    def test_mixed_new_and_old(self):
        entry = {"alert": True, "campground_id": 100}
        results = [
            make_campsite(campsite_id=1, booking_date=datetime(2026, 7, 4)),
            make_campsite(campsite_id=2, booking_date=datetime(2026, 7, 4)),
        ]
        processed = process_results(entry, results, None)
        prev = {make_notification_key(entry, processed.facility_name, results[0])}
        new = filter_new_availability(processed, prev)
        assert new.total_sites == 1
        assert new.campsites[0].campsite_id == 2

    def test_facility_rename_does_not_reset_dedup(self):
        """Keys are based on config identity, so a renamed facility stays deduped."""
        entry = {"alert": True, "campground_id": 100}
        original = process_results(
            entry, [make_campsite(campsite_id=1, facility_name="Old Name")], None
        )
        prev = set(original.notification_keys)
        renamed = process_results(
            entry, [make_campsite(campsite_id=1, facility_name="New Name")], None
        )
        assert not filter_new_availability(renamed, prev).available


# ── send_telegram ───────────────────────────────────────────────────────────


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.telegram.org/botTOKEN/sendMessage",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(b"{}"),
    )


class TestSendTelegram:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr("campsite_checker.notify.time.sleep", lambda _s: None)

    def test_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(
            "campsite_checker.notify.urllib.request.urlopen",
            lambda req, timeout: io.BytesIO(b"{}"),
        )
        assert send_telegram("tok", "chat", "hello") is True

    def test_non_retryable_http_error_returns_false(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req)
            raise _http_error(400)

        monkeypatch.setattr("campsite_checker.notify.urllib.request.urlopen", fake_urlopen)
        assert send_telegram("tok", "chat", "hello") is False
        assert len(calls) == 1  # 400 is not retried

    def test_rate_limit_retries_then_succeeds(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req)
            if len(calls) < 3:
                raise _http_error(429)
            return io.BytesIO(b"{}")

        monkeypatch.setattr("campsite_checker.notify.urllib.request.urlopen", fake_urlopen)
        assert send_telegram("tok", "chat", "hello") is True
        assert len(calls) == 3

    def test_network_errors_exhaust_retries_and_return_false(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req)
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("campsite_checker.notify.urllib.request.urlopen", fake_urlopen)
        assert send_telegram("tok", "chat", "hello") is False
        assert len(calls) == 3
