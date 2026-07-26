from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from campsite_checker.throttle import (
    ProviderThrottleRegistry,
    _retry_after_seconds,
    detect_rate_limit,
)


class FakeClock:
    def __init__(self, now: float = 100):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_adaptive_backoff_doubles_and_caps():
    clock = FakeClock()
    registry = ProviderThrottleRegistry(
        base_delay_seconds=30,
        max_delay_seconds=90,
        clock=clock,
    )

    assert registry.record_rate_limit("RecreationDotGov") == 30
    clock.now += 30
    assert registry.record_rate_limit("RecreationDotGov") == 60
    clock.now += 60
    assert registry.record_rate_limit("RecreationDotGov") == 90

    snapshot = registry.snapshot()[0]
    assert snapshot.rate_limit_events == 3
    assert snapshot.consecutive_rate_limits == 3
    assert snapshot.cooldown_seconds == 90
    assert snapshot.last_backoff_seconds == 90


def test_retry_after_is_honored_even_beyond_backoff_cap():
    registry = ProviderThrottleRegistry(
        base_delay_seconds=30,
        max_delay_seconds=300,
        clock=FakeClock(),
    )

    assert registry.record_rate_limit("ReserveCalifornia", retry_after_seconds=600) == 600


def test_only_success_started_after_rate_limit_resets_streak():
    clock = FakeClock()
    registry = ProviderThrottleRegistry(clock=clock)
    request_started_before_limit = clock.now - 1
    registry.record_rate_limit("RecreationDotGov")

    registry.record_success(
        "RecreationDotGov",
        request_started_at=request_started_before_limit,
    )
    assert registry.snapshot()[0].consecutive_rate_limits == 1

    clock.now += 31
    registry.record_success("RecreationDotGov", request_started_at=clock.now)
    assert registry.snapshot()[0].consecutive_rate_limits == 0


def test_detects_wrapped_429_and_retry_after():
    response = SimpleNamespace(status_code=429, headers={"Retry-After": "75"})
    http_error = RuntimeError("request failed")
    http_error.response = response
    wrapped = RuntimeError("provider retries exhausted")
    wrapped.last_attempt = SimpleNamespace(exception=lambda: http_error)

    detection = detect_rate_limit(wrapped)

    assert detection.rate_limited is True
    assert detection.retry_after_seconds == 75


def test_detects_rate_limit_message_without_response():
    detection = detect_rate_limit(RuntimeError("429 Client Error: Too Many Requests"))

    assert detection.rate_limited is True
    assert detection.retry_after_seconds is None


def test_parses_http_date_retry_after():
    now = datetime.now(timezone.utc)
    retry_at = now + timedelta(seconds=90)

    delay = _retry_after_seconds(
        retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        now=now,
    )

    assert delay == pytest.approx(90, abs=1)
