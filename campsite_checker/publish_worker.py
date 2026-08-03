"""Non-blocking, coalescing dashboard publication.

Dashboard rendering and object-storage uploads are presentation work, not part
of the latency-sensitive alert path. This worker owns a single
``DashboardPublisher`` instance and keeps at most one pending snapshot: when a
newer snapshot arrives while publication is busy, the older pending snapshot
is replaced.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable

from .results import ProcessedAvailability

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DashboardPublishOutcome:
    result: Any | None
    duration_seconds: float
    completed_at: datetime
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _PublishRequest:
    availabilities: list[ProcessedAvailability]
    search_filter: Any | None
    scan_schedule: Any | None


class DashboardPublishWorker:
    """Publish the newest dashboard snapshot without blocking its producer."""

    def __init__(
        self,
        publisher: Any,
        *,
        on_start: Callable[[], None] | None = None,
        on_complete: Callable[[DashboardPublishOutcome], None] | None = None,
    ):
        self._publisher = publisher
        self._on_start = on_start
        self._on_complete = on_complete
        self._cond = threading.Condition()
        self._pending: _PublishRequest | None = None
        self._in_progress = False
        self._shutdown = False
        # A stuck network call must not keep interpreter shutdown alive. Normal
        # service shutdown still asks the worker to stop and discards pending
        # presentation work; the next process publishes a fresh snapshot.
        self._thread = threading.Thread(
            target=self._run,
            name="dashboard-publisher",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        availabilities: list[ProcessedAvailability],
        *,
        search_filter: Any | None = None,
        scan_schedule: Any | None = None,
    ) -> bool:
        """Queue the latest snapshot and return whether an older one was replaced."""
        request = _PublishRequest(list(availabilities), search_filter, scan_schedule)
        with self._cond:
            if self._shutdown:
                raise RuntimeError("DashboardPublishWorker is shut down")
            replaced = self._pending is not None
            self._pending = request
            self._cond.notify_all()
            return replaced

    @property
    def in_progress(self) -> bool:
        with self._cond:
            return self._in_progress

    @property
    def has_pending(self) -> bool:
        with self._cond:
            return self._pending is not None

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no publication is running or pending; primarily for tests."""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._in_progress and self._pending is None,
                timeout=timeout,
            )

    def shutdown(self, *, wait: bool = False, timeout: float | None = None) -> None:
        with self._cond:
            self._shutdown = True
            self._pending = None
            self._cond.notify_all()
        if wait:
            self._thread.join(timeout=timeout)

    def _safe_callback(self, callback: Callable | None, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Dashboard publication callback failed")

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._shutdown or self._pending is not None)
                if self._shutdown:
                    return
                request = self._pending
                self._pending = None
                self._in_progress = True

            self._safe_callback(self._on_start)
            started = monotonic()
            try:
                result = self._publisher.publish(
                    request.availabilities,
                    search_filter=request.search_filter,
                    scan_schedule=request.scan_schedule,
                )
                error = None
            except BaseException as exc:
                result = None
                error = exc
            outcome = DashboardPublishOutcome(
                result=result,
                duration_seconds=max(0.0, monotonic() - started),
                completed_at=datetime.now(timezone.utc),
                error=error,
            )

            self._safe_callback(self._on_complete, outcome)
            with self._cond:
                self._in_progress = False
                self._cond.notify_all()
