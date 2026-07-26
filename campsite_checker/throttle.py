import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable


@dataclass(frozen=True, slots=True)
class RateLimitDetection:
    rate_limited: bool
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderThrottleSnapshot:
    provider: str
    rate_limit_events: int
    consecutive_rate_limits: int
    cooldown_seconds: float
    last_backoff_seconds: float


@dataclass(slots=True)
class _ProviderThrottleState:
    rate_limit_events: int = 0
    consecutive_rate_limits: int = 0
    cooldown_until: float = 0
    last_backoff_seconds: float = 0
    last_rate_limit_at: float = 0


class ProviderThrottleRegistry:
    """Process-wide adaptive cooldown state, isolated by provider."""

    def __init__(
        self,
        *,
        base_delay_seconds: float = 30,
        max_delay_seconds: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        if base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be greater than zero")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")
        self.base_delay_seconds = float(base_delay_seconds)
        self.max_delay_seconds = float(max_delay_seconds)
        self._clock = clock
        self._states: dict[str, _ProviderThrottleState] = {}
        self._lock = threading.Lock()

    def ensure(self, provider: str) -> None:
        with self._lock:
            self._states.setdefault(provider, _ProviderThrottleState())

    def cooldown_seconds(self, provider: str) -> float:
        with self._lock:
            state = self._states.get(provider)
            if state is None:
                return 0
            return max(0.0, state.cooldown_until - self._clock())

    def record_rate_limit(
        self,
        provider: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> float:
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(provider, _ProviderThrottleState())
            state.rate_limit_events += 1
            state.consecutive_rate_limits += 1
            exponential_delay = self.base_delay_seconds
            for _attempt in range(state.consecutive_rate_limits - 1):
                exponential_delay = min(self.max_delay_seconds, exponential_delay * 2)
                if exponential_delay >= self.max_delay_seconds:
                    break
            requested_delay = max(0.0, retry_after_seconds or 0.0)
            delay = max(exponential_delay, requested_delay)
            state.cooldown_until = max(state.cooldown_until, now + delay)
            state.last_backoff_seconds = delay
            state.last_rate_limit_at = now
            return delay

    def record_success(self, provider: str, *, request_started_at: float) -> None:
        """Reset the backoff streak after a request begun after the latest limit succeeds."""
        with self._lock:
            state = self._states.setdefault(provider, _ProviderThrottleState())
            if request_started_at > state.last_rate_limit_at:
                state.consecutive_rate_limits = 0

    def snapshot(self) -> tuple[ProviderThrottleSnapshot, ...]:
        now = self._clock()
        with self._lock:
            return tuple(
                ProviderThrottleSnapshot(
                    provider=provider,
                    rate_limit_events=state.rate_limit_events,
                    consecutive_rate_limits=state.consecutive_rate_limits,
                    cooldown_seconds=max(0.0, state.cooldown_until - now),
                    last_backoff_seconds=state.last_backoff_seconds,
                )
                for provider, state in sorted(self._states.items())
            )


def _retry_after_seconds(value, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (retry_at - current).total_seconds())


def _nested_exceptions(exc: BaseException):
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current

        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(arg for arg in current.args if isinstance(arg, BaseException))

        last_attempt = getattr(current, "last_attempt", None)
        exception_method = getattr(last_attempt, "exception", None)
        if callable(exception_method):
            nested = exception_method()
            if isinstance(nested, BaseException):
                pending.append(nested)


def detect_rate_limit(exc: BaseException) -> RateLimitDetection:
    """Detect HTTP 429 responses, including exceptions wrapped by retry libraries."""
    fallback_match = False
    for current in _nested_exceptions(exc):
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(current, "code", None)
        if status == 429:
            headers = getattr(response, "headers", None) or getattr(current, "headers", None) or {}
            return RateLimitDetection(
                rate_limited=True,
                retry_after_seconds=_retry_after_seconds(headers.get("Retry-After")),
            )
        fallback_match = fallback_match or bool(
            re.search(r"\b429\b|too many requests|rate.?limit", str(current), re.IGNORECASE)
        )
    return RateLimitDetection(rate_limited=fallback_match)


def _positive_env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


_BASE_DELAY_SECONDS = _positive_env_float("THROTTLE_BASE_DELAY", 30)
_MAX_DELAY_SECONDS = max(
    _BASE_DELAY_SECONDS,
    _positive_env_float("THROTTLE_MAX_DELAY", 15 * 60),
)

PROVIDER_THROTTLES = ProviderThrottleRegistry(
    base_delay_seconds=_BASE_DELAY_SECONDS,
    max_delay_seconds=_MAX_DELAY_SECONDS,
)
