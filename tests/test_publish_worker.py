"""Tests for isolated, coalescing dashboard publication."""

import threading
from types import SimpleNamespace

from campsite_checker.publish_worker import DashboardPublishWorker


def test_slow_publication_does_not_block_submitter():
    started = threading.Event()
    release = threading.Event()

    class SlowPublisher:
        def publish(self, availabilities, search_filter=None):
            started.set()
            assert release.wait(timeout=2)
            return SimpleNamespace(uploaded=True)

    worker = DashboardPublishWorker(SlowPublisher())
    try:
        worker.submit(["snapshot"])
        assert started.wait(timeout=2)
        # The producer remains free while publication is blocked.
        assert worker.submit(["newer"]) is False
    finally:
        release.set()
        assert worker.wait_idle(timeout=2)
        worker.shutdown(wait=True, timeout=2)


def test_pending_snapshots_are_coalesced_to_the_newest():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class SlowPublisher:
        def publish(self, availabilities, search_filter=None):
            calls.append(list(availabilities))
            if len(calls) == 1:
                started.set()
                assert release.wait(timeout=2)
            return SimpleNamespace(uploaded=True)

    worker = DashboardPublishWorker(SlowPublisher())
    try:
        worker.submit(["first"])
        assert started.wait(timeout=2)
        assert worker.submit(["obsolete"]) is False
        assert worker.submit(["latest"]) is True
        release.set()
        assert worker.wait_idle(timeout=2)
        assert calls == [["first"], ["latest"]]
    finally:
        release.set()
        worker.shutdown(wait=True, timeout=2)


def test_worker_reports_failures_without_dying():
    outcomes = []

    class FailingPublisher:
        def publish(self, availabilities, search_filter=None):
            raise RuntimeError("R2 unavailable")

    worker = DashboardPublishWorker(FailingPublisher(), on_complete=outcomes.append)
    try:
        worker.submit(["snapshot"])
        assert worker.wait_idle(timeout=2)
        assert len(outcomes) == 1
        assert isinstance(outcomes[0].error, RuntimeError)
    finally:
        worker.shutdown(wait=True, timeout=2)
