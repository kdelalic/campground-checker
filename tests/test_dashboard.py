"""Tests for dashboard rendering from normalized availability."""

from datetime import datetime, timezone
from types import SimpleNamespace

from campsite_checker.dashboard import DashboardPublisher, build_dashboard_html
from campsite_checker.results import process_filtered_results, process_results

from .conftest import make_campsite


def test_dashboard_reuses_preprocessed_availability(monkeypatch):
    processed = process_results(
        {"name": "Configured Name", "campground_id": 100},
        [make_campsite(booking_url="https://example.com/book")],
        None,
    )

    def fail_if_reprocessed(*args, **kwargs):
        raise AssertionError("processed availability should not be filtered again")

    monkeypatch.setattr("campsite_checker.dashboard.process_results", fail_if_reprocessed)

    content = build_dashboard_html(
        [processed],
        None,
        scan_timestamp=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )

    assert "Test Area — Test Campground" in content
    assert "1 open site(s)" in content
    assert "https://example.com/book" in content


def render(availabilities):
    return build_dashboard_html(
        availabilities,
        None,
        scan_timestamp=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )


class TestFailedScanRendering:
    """A failed search says nothing about a campground, so it must not render
    like a search that ran and found nothing."""

    def test_failed_scan_is_distinct_from_no_availability(self):
        failed = process_filtered_results(
            {"name": "Kirk Creek", "campground_id": 233116}, [], search_succeeded=False
        )

        content = render([failed])

        assert "Scan failed" in content
        assert '<div class="card card-unavailable card-failed"' in content
        assert "No availability" not in content

    def test_empty_successful_scan_still_reads_as_no_availability(self):
        empty = process_filtered_results({"name": "North Pines", "campground_id": 1}, [])

        content = render([empty])

        assert "No availability" in content
        assert "Scan failed" not in content
        assert '<div class="card card-unavailable card-failed"' not in content

    def test_failed_scan_marked_in_quick_nav(self):
        failed = process_filtered_results({"name": "Kirk Creek"}, [], search_succeeded=False)
        ok = process_filtered_results({"name": "North Pines"}, [])

        content = render([failed, ok])

        assert 'data-ref="site-0" data-unavailable="true" data-failed="true"' in content
        assert 'data-ref="site-1" data-unavailable="true">' in content
        assert "nav-failed" in content

    def test_partial_results_keep_data_but_flag_staleness(self):
        """Entries whose searches partly failed still have real results; the
        card shows them alongside a warning rather than hiding them."""
        partial = process_filtered_results(
            {"name": "Emerald Bay"}, [make_campsite()], search_succeeded=False
        )

        content = render([partial])

        assert "Partial scan" in content
        assert '<div class="card card-partial"' in content
        assert "1 open site(s)" in content

    def test_failure_count_shown_in_summary_stats(self):
        content = render(
            [
                process_filtered_results({"name": "Upper Pines"}, [make_campsite()]),
                process_filtered_results({"name": "North Pines"}, []),
                process_filtered_results({"name": "Kirk Creek"}, [], search_succeeded=False),
            ]
        )

        assert "<strong>1</strong> of 3 campgrounds with availability" in content
        assert "1 scan(s) failed" in content


class TestSummaryStats:
    def test_soonest_available_date_reported(self):
        availability = process_filtered_results(
            {"name": "Upper Pines"},
            [
                make_campsite(campsite_id=1, booking_date=datetime(2026, 9, 12)),
                make_campsite(campsite_id=2, booking_date=datetime(2026, 8, 3)),
            ],
        )

        content = render([availability])

        assert "soonest <strong>Mon, Aug 3</strong>" in content

    def test_no_stat_line_without_entries(self):
        content = render([])

        assert '<p class="stat-line"' not in content
        assert "No availability found in the current scan." in content


class TestPerSiteDetail:
    def test_date_row_expands_to_individual_sites(self):
        availability = process_filtered_results(
            {"name": "Upper Pines"},
            [
                make_campsite(
                    campsite_id=1,
                    campsite_site_name="A012",
                    campsite_loop_name="Loop A",
                    booking_url="https://example.com/site/1",
                ),
                make_campsite(
                    campsite_id=2,
                    campsite_site_name="B004",
                    campsite_loop_name="Loop B",
                    booking_url="https://example.com/site/2",
                ),
            ],
        )

        content = render([availability])

        assert "<details" in content
        assert "<summary>Sat, Jul 4</summary>" in content
        assert 'href="https://example.com/site/1"' in content
        assert ">A012</a>" in content
        assert ">B004</a>" in content
        assert "Loop A" in content
        assert "2 site(s)" in content

    def test_detail_lives_inside_the_filterable_row(self):
        """The date filter hides whole <tr>s by data-date, so the disclosure has
        to sit inside the row it belongs to."""
        availability = process_filtered_results({"name": "Upper Pines"}, [make_campsite()])

        content = render([availability])

        row_start = content.index('<tr data-date="2026-07-04"')
        row_end = content.index("</tr>", row_start)
        row = content[row_start:row_end]
        assert "<details" in row
        assert "</details>" in row

    def test_site_without_name_falls_back_to_id(self):
        availability = process_filtered_results(
            {"name": "Upper Pines"},
            [make_campsite(campsite_id=77, campsite_site_name="", campsite_loop_name="")],
        )

        content = render([availability])

        assert "Site 77" in content

    def test_site_metadata_is_escaped(self):
        availability = process_filtered_results(
            {"name": "Upper Pines"},
            [make_campsite(campsite_site_name="<script>x</script>", campsite_loop_name="A & B")],
        )

        content = render([availability])

        assert "<script>x</script>" not in content
        assert "&lt;script&gt;x&lt;/script&gt;" in content
        assert "A &amp; B" in content


class TestRefreshAndAccessibility:
    def test_page_refreshes_itself_only_while_visible(self):
        content = render([process_filtered_results({}, [make_campsite()])])

        assert 'document.visibilityState === "visible"' in content
        assert "window.location.reload()" in content

    def test_refresh_interval_is_configurable(self):
        content = build_dashboard_html(
            [process_filtered_results({}, [make_campsite()])],
            None,
            scan_timestamp=datetime(2026, 7, 26, tzinfo=timezone.utc),
            refresh_seconds=42,
        )

        assert "const REFRESH_MS = 42 * 1000;" in content

    def test_calendar_days_are_keyboard_operable(self):
        content = render([process_filtered_results({}, [make_campsite()])])

        assert 'tabindex="0" role="button"' in content
        assert 'aria-label="July 4, 2026' in content
        assert 'e.key === "Enter"' in content

    def test_page_stays_self_contained(self):
        content = render([process_filtered_results({}, [make_campsite()])])

        assert "<script src=" not in content
        assert "<link " not in content
        assert "@import" not in content


class FakeUploader:
    def __init__(self, successes):
        self.successes = iter(successes)
        self.calls = 0

    def upload(self, output_path):
        self.calls += 1
        return SimpleNamespace(
            success=next(self.successes),
            public_url="https://example.com/dashboard",
        )


def test_publisher_skips_unchanged_write_and_upload(tmp_path):
    output_path = tmp_path / "dashboard.html"
    uploader = FakeUploader([True])
    publisher = DashboardPublisher(str(output_path), uploader)
    processed = process_results({}, [make_campsite(campsite_id=1)], None)

    first = publisher.publish([processed])
    second = publisher.publish([processed])

    assert first.written is True
    assert first.uploaded is True
    assert second.written is False
    assert second.uploaded is False
    assert uploader.calls == 1


def test_publisher_retries_failed_upload_without_rewriting(tmp_path):
    output_path = tmp_path / "dashboard.html"
    uploader = FakeUploader([False, True])
    publisher = DashboardPublisher(str(output_path), uploader)
    processed = process_results({}, [make_campsite(campsite_id=1)], None)

    first = publisher.publish([processed])
    second = publisher.publish([processed])

    assert first.written is True
    assert first.uploaded is False
    assert second.written is False
    assert second.uploaded is True
    assert uploader.calls == 2


def test_publisher_rewrites_when_availability_changes(tmp_path):
    output_path = tmp_path / "dashboard.html"
    publisher = DashboardPublisher(str(output_path))
    first = process_results({}, [make_campsite(campsite_id=1)], None)
    changed = process_results({}, [make_campsite(campsite_id=2)], None)

    assert publisher.publish([first]).written is True
    assert publisher.publish([changed]).written is True


def test_publisher_rewrites_when_a_failed_scan_recovers(tmp_path):
    """Failure and success both render zero dates; only search_succeeded tells
    them apart, so the fingerprint has to notice the transition."""
    output_path = tmp_path / "dashboard.html"
    publisher = DashboardPublisher(str(output_path))
    failed = process_filtered_results({"name": "Kirk Creek"}, [], search_succeeded=False)
    recovered = process_filtered_results({"name": "Kirk Creek"}, [])

    assert publisher.publish([failed]).written is True
    assert publisher.publish([failed]).written is False
    assert publisher.publish([recovered]).written is True
    assert "Scan failed" not in output_path.read_text()


def test_publisher_republishes_unchanged_content_once_stale(tmp_path):
    """The page's "Last updated" is its only liveness signal, so unchanged
    availability is still republished after the freshness interval."""
    output_path = tmp_path / "dashboard.html"
    clock_now = [0.0]
    uploader = FakeUploader([True, True])
    publisher = DashboardPublisher(
        str(output_path),
        uploader,
        freshness_interval_seconds=3600,
        clock=lambda: clock_now[0],
    )
    processed = process_results({}, [make_campsite(campsite_id=1)], None)

    assert publisher.publish([processed]).written is True
    clock_now[0] = 1800.0
    mid = publisher.publish([processed])
    assert mid.written is False
    assert mid.uploaded is False
    clock_now[0] = 3700.0
    stale = publisher.publish([processed])
    assert stale.written is True
    assert stale.uploaded is True
    assert uploader.calls == 2
