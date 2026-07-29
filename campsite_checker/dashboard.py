from __future__ import annotations

import calendar
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from camply.containers import AvailableCampsite
from jinja2 import Environment, PackageLoader

from .results import (
    ProcessedAvailability,
    availability_fingerprint,
    count_matching_dates,
    entry_identity,
    process_results,
)
from .weekdays import WEEKDAY_LABELS

# How often an already-open browser tab re-fetches the page. The checker
# republishes on its own cadence, so this only bounds how stale a left-open tab
# can get.
DEFAULT_REFRESH_SECONDS = 300
DEFAULT_STALE_AFTER_SECONDS = 2 * 60 * 60

TEMPLATE_DIR = "templates"
PAGE_TEMPLATE = "dashboard.html.j2"


@dataclass(frozen=True, slots=True)
class DashboardPublishResult:
    written: bool
    uploaded: bool
    public_url: str | None = None


class DashboardPublisher:
    """Write and upload a dashboard when availability changes or freshness lapses.

    Skipping unchanged rewrites keeps R2 operations cheap, but the page's
    "Last updated" timestamp is its only liveness signal, so a periodic
    freshness republish keeps a quiet week distinguishable from a dead checker.
    """

    def __init__(
        self,
        output_path: str,
        uploader: Any = None,
        freshness_interval_seconds: float = 60 * 60,
        clock=time.monotonic,
    ):
        self.output_path = output_path
        self.uploader = uploader
        self.freshness_interval_seconds = freshness_interval_seconds
        self._clock = clock
        self.last_written_fingerprint: str | None = None
        self.last_uploaded_fingerprint: str | None = None
        self._last_written_at: float | None = None
        self._last_uploaded_at: float | None = None
        self._successful_snapshots: dict[
            tuple[str, str], tuple[ProcessedAvailability, datetime]
        ] = {}

    def _is_stale(self, last_at: float | None, now: float) -> bool:
        return last_at is None or (now - last_at) >= self.freshness_interval_seconds

    def publish(
        self,
        availabilities: list[ProcessedAvailability],
        day_filter: set[int] | None = None,
        search_filter: SearchFilterView | None = None,
    ) -> DashboardPublishResult:
        rendered_availabilities = self._retain_last_successful(availabilities)
        fingerprint = availability_fingerprint(rendered_availabilities)
        if search_filter is not None:
            # The rendered filter is part of the page, and its date range rolls
            # forward daily, so an unchanged availability set must still
            # republish when the range moves.
            fingerprint = f"{fingerprint}|{search_filter.fingerprint}"
        now = self._clock()
        written = (
            fingerprint != self.last_written_fingerprint
            or not Path(self.output_path).exists()
            or self._is_stale(self._last_written_at, now)
        )
        if written:
            generate_dashboard(
                rendered_availabilities,
                day_filter,
                self.output_path,
                search_filter,
                stale_after_seconds=max(
                    DEFAULT_REFRESH_SECONDS * 2,
                    int(self.freshness_interval_seconds * 2),
                ),
            )
            self.last_written_fingerprint = fingerprint
            self._last_written_at = now

        uploaded = False
        public_url = None
        if self.uploader is not None and (
            fingerprint != self.last_uploaded_fingerprint
            or self._is_stale(self._last_uploaded_at, now)
        ):
            upload_result = self.uploader.upload(self.output_path)
            if upload_result.success:
                uploaded = True
                public_url = upload_result.public_url
                self.last_uploaded_fingerprint = fingerprint
                self._last_uploaded_at = now

        return DashboardPublishResult(
            written=written,
            uploaded=uploaded,
            public_url=public_url,
        )

    def _retain_last_successful(
        self,
        availabilities: list[ProcessedAvailability],
    ) -> list[ProcessedAvailability]:
        """Use a labeled prior snapshot when a campground cannot be checked.

        This cache deliberately lives at the presentation boundary: alerts,
        metrics, and health reporting must continue to reflect the current
        failed scan rather than the dashboard's last-known-good fallback.
        """
        now = datetime.now(timezone.utc).astimezone()
        rendered = []
        for availability in availabilities:
            configured_identity = availability.entry.get("_config_index")
            if configured_identity is None:
                configured_identity = entry_identity(
                    availability.entry,
                    availability.entry.get("name", ""),
                )
            key = (
                availability.entry.get("provider", "RecreationDotGov"),
                str(configured_identity),
            )
            if availability.search_succeeded:
                clean = replace(availability, last_successful_at=None)
                self._successful_snapshots[key] = (clean, now)
                rendered.append(clean)
                continue

            previous = self._successful_snapshots.get(key)
            if previous is not None and not availability.available:
                snapshot, successful_at = previous
                rendered.append(
                    replace(
                        snapshot,
                        entry=availability.entry,
                        search_succeeded=False,
                        last_successful_at=successful_at,
                    )
                )
            else:
                rendered.append(availability)
        return rendered


def get_dashboard_path(args, config: dict) -> str | None:
    """Resolve dashboard output path.

    Priority: --no-dashboard > --dashboard CLI arg > dashboard.output_path in YAML > None.
    """
    if getattr(args, "no_dashboard", False):
        return None
    cli_path = getattr(args, "dashboard", None)
    if cli_path is not None:
        return cli_path
    dash_cfg = config.get("dashboard") or {}
    return dash_cfg.get("output_path")


# ── Static assets and template environment ──────────────────────────────────


@lru_cache(maxsize=None)
def read_asset(name: str) -> str:
    """Read a static asset shipped alongside the templates.

    Cached because the checker renders the page on every scan for the life of
    the process, and the assets never change under it.
    """
    return files(__package__).joinpath(TEMPLATE_DIR).joinpath(name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def template_environment() -> Environment:
    return Environment(
        loader=PackageLoader(__package__, TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


# ── View models ─────────────────────────────────────────────────────────────
#
# The template only ever sees these: every value is already resolved to what
# should appear on the page, and Jinja's autoescaping handles the escaping that
# used to be applied by hand at each interpolation.


@dataclass(frozen=True, slots=True)
class SiteView:
    name: str
    loop: str
    url: str


@dataclass(frozen=True, slots=True)
class DateRowView:
    iso: str
    label: str
    count: int
    sites: tuple[SiteView, ...]


@dataclass(frozen=True, slots=True)
class CardView:
    id: str
    name: str
    state: str  # "available" | "empty" | "failed"
    total: int
    rows: tuple[DateRowView, ...]
    booking_url: str
    partial: bool = False
    # Pre-formatted, e.g. "2 nights". Per-card rather than in the page header
    # because entries may disagree, and an entry with `criteria` searches
    # several stay lengths at once.
    nights: str = ""
    unique_sites: int = 0
    date_count: int = 0
    stale_label: str = ""
    stale_timestamp_iso: str = ""

    @property
    def css_class(self) -> str:
        if self.state == "failed":
            return "card card-unavailable card-failed"
        if self.state == "empty":
            return "card card-unavailable"
        return "card card-partial" if self.partial else "card"

    @property
    def availability_label(self) -> str:
        if self.state == "empty":
            return "No availability"
        opening_label = "opening" if self.total == 1 else "openings"
        date_label = "date" if self.date_count == 1 else "dates"
        site_label = "site" if self.unique_sites == 1 else "unique sites"
        return (
            f"{self.total} {opening_label} across {self.date_count} {date_label}"
            f" · {self.unique_sites} {site_label}"
        )


@dataclass(frozen=True, slots=True)
class CalendarCell:
    kind: str  # "available" | "searched" | "day" | "empty"
    day: int = 0
    iso: str = ""
    count: int = 0
    label: str = ""
    # Rendered into the cell so the date-filter banner can name the selected
    # day without the static JS asset having to format dates itself.
    short_label: str = ""


@dataclass(frozen=True, slots=True)
class CalendarMonth:
    id: str
    label: str
    year_month: str
    weeks: tuple[tuple[CalendarCell, ...], ...]


@dataclass(frozen=True, slots=True)
class StatsView:
    available: int
    total: int
    soonest: str
    failed: int


@dataclass(frozen=True, slots=True)
class SearchFilterView:
    """The criteria the scan actually ran with.

    Without this, "No availability" on the page is ambiguous: it could mean
    nothing is open at all, or that nothing is open on the days and stay
    lengths being searched. ``nights`` summarises every entry on the page; the
    per-card badge stays authoritative when they disagree.
    """

    days: str
    date_range: str
    dates: int
    nights: str = ""
    start: date | None = None
    end: date | None = None
    day_filter: frozenset[int] | None = None

    @property
    def fingerprint(self) -> str:
        """Identity for change detection; the range rolls forward daily."""
        return (
            f"{self.days}|{self.nights}|{self.date_range}|{self.dates}"
            f"|{self.start}|{self.end}|{self.day_filter}"
        )


def entry_nights(entry: dict) -> list[int]:
    """Stay lengths searched for one entry, shortest first.

    `runner.run_once` stamps the resolved values on the entry, because the
    effective nights depend on the `--nights` override and on any `criteria`,
    neither of which is visible from the entry's own `nights` key.
    """
    nights = entry.get("_searched_nights") or [entry.get("nights", 1)]
    return sorted({int(value) for value in nights})


def format_nights(values: Iterable[int]) -> str:
    """Render stay lengths as a label, e.g. "2 nights" or "1 / 3 nights"."""
    unique = sorted(set(values))
    if not unique:
        return ""
    label = " / ".join(str(value) for value in unique)
    return f"{label} night" if unique == [1] else f"{label} nights"


def build_nights_label(entry: dict) -> str:
    """Format the stay lengths searched for one entry."""
    return format_nights(entry_nights(entry))


def build_search_filter_view(
    day_filter: set[int] | None,
    start_dt: datetime | date,
    end_dt: datetime | date,
    availabilities: Iterable[ProcessedAvailability] = (),
) -> SearchFilterView:
    """Describe a scan's day filter, stay length, and date range for the header.

    Nights are summarised from the availabilities being rendered rather than
    from the config, so the header can never disagree with the cards below it.
    """
    if day_filter is None:
        days = "All days"
    else:
        days = ", ".join(WEEKDAY_LABELS[day] for day in sorted(day_filter))
    start = start_dt.date() if isinstance(start_dt, datetime) else start_dt
    end = end_dt.date() if isinstance(end_dt, datetime) else end_dt
    midnight = datetime.min.time()
    return SearchFilterView(
        days=days,
        date_range=f"{start.strftime('%b %-d, %Y')} – {end.strftime('%b %-d, %Y')}",
        # `count_matching_dates` takes datetimes; callers may hold either.
        dates=count_matching_dates(
            datetime.combine(start, midnight),
            datetime.combine(end, midnight),
            day_filter,
        ),
        nights=format_nights(
            night for availability in availabilities for night in entry_nights(availability.entry)
        ),
        start=start,
        end=end,
        day_filter=frozenset(day_filter) if day_filter is not None else None,
    )


def build_calendar_months(
    all_availabilities: dict[date, int],
    start: date | None = None,
    end: date | None = None,
    day_filter: frozenset[int] | set[int] | None = None,
) -> list[CalendarMonth]:
    """Lay out the complete searched range, including dates with no results."""
    if start is None and not all_availabilities:
        return []

    min_date = start or min(all_availabilities)
    # Search windows are end-exclusive. Without an explicit range, retain the
    # historic behavior of ending on the last result date.
    max_date = end - timedelta(days=1) if end is not None else max(all_availabilities)
    if max_date < min_date:
        return []
    cal = calendar.Calendar(firstweekday=6)  # Sunday start

    months: list[CalendarMonth] = []
    curr_year, curr_month = min_date.year, min_date.month
    while (curr_year, curr_month) <= (max_date.year, max_date.month):
        weeks = []
        for week in cal.monthdatescalendar(curr_year, curr_month):
            cells = []
            for d in week:
                count = all_availabilities.get(d, 0)
                if d.month != curr_month:
                    cells.append(CalendarCell(kind="empty"))
                elif count > 0:
                    cells.append(
                        CalendarCell(
                            kind="available",
                            day=d.day,
                            iso=d.isoformat(),
                            count=count,
                            label=f"{d.strftime('%B %-d, %Y')} — {count} site(s) available",
                            short_label=d.strftime("%a, %b %-d"),
                        )
                    )
                elif min_date <= d <= max_date and (
                    day_filter is None or d.weekday() in day_filter
                ):
                    cells.append(
                        CalendarCell(
                            kind="searched",
                            day=d.day,
                            iso=d.isoformat(),
                            label=f"{d.strftime('%B %-d, %Y')} — searched, no availability",
                        )
                    )
                else:
                    cells.append(CalendarCell(kind="day", day=d.day))
            weeks.append(tuple(cells))

        months.append(
            CalendarMonth(
                id=f"cal-month-{len(months)}",
                label=f"{calendar.month_name[curr_month]} {curr_year}",
                year_month=f"{curr_year:04d}-{curr_month:02d}",
                weeks=tuple(weeks),
            )
        )

        curr_month += 1
        if curr_month > 12:
            curr_month, curr_year = 1, curr_year + 1

    return months


def group_sites_by_date(
    campsites: tuple[AvailableCampsite, ...],
) -> dict[date, list[AvailableCampsite]]:
    """Group already-deduplicated campsites by their booking date."""
    grouped: dict[date, list[AvailableCampsite]] = defaultdict(list)
    for site in campsites:
        grouped[site.booking_date.date()].append(site)
    for sites in grouped.values():
        sites.sort(
            key=lambda s: (str(getattr(s, "campsite_site_name", "") or ""), str(s.campsite_id))
        )
    return grouped


def build_site_views(sites: list[AvailableCampsite]) -> tuple[SiteView, ...]:
    views = []
    for site in sites:
        name = str(getattr(site, "campsite_site_name", "") or "") or f"Site {site.campsite_id}"
        url = str(getattr(site, "booking_url", "") or "").replace("Web/Default.aspx#!", "")
        views.append(
            SiteView(
                name=name,
                loop=str(getattr(site, "campsite_loop_name", "") or ""),
                url=url,
            )
        )
    return tuple(views)


def build_card_view(availability: ProcessedAvailability, card_id: str) -> CardView:
    """Turn one campground's normalized availability into a renderable card."""
    entry = availability.entry
    # Resolve the display name: use camply result metadata when available,
    # otherwise fall back to the entry's config-level name.
    if availability.available:
        name = availability.facility_name
    else:
        name = entry.get("name") or f"Campground #{entry.get('campground_id', '?')}"

    scan_failed = not availability.search_succeeded
    stale_label = ""
    stale_timestamp_iso = ""
    if availability.last_successful_at is not None:
        successful_at = availability.last_successful_at
        if successful_at.tzinfo is None:
            successful_at = successful_at.replace(tzinfo=timezone.utc).astimezone()
        stale_timestamp_iso = successful_at.isoformat()
        stale_label = (
            f"Showing last successful data from {successful_at.strftime('%b %-d at %-I:%M %p')}"
        )
    elif scan_failed and availability.available:
        stale_label = "Some provider requests failed; these results may be incomplete"

    if not availability.available:
        # A failed search carries no information about this campground, so it
        # must not read as "nothing open here".
        return CardView(
            id=card_id,
            name=name,
            state="failed" if scan_failed else "empty",
            total=0,
            rows=(),
            booking_url="",
            # An empty card is exactly where the stay length matters most: it
            # says what "no availability" was actually checked against.
            nights=build_nights_label(entry),
            stale_label=stale_label,
            stale_timestamp_iso=stale_timestamp_iso,
        )

    sites_by_date = group_sites_by_date(availability.campsites)
    rows = tuple(
        DateRowView(
            iso=d.isoformat(),
            label=d.strftime("%a, %b %-d"),
            count=len(availability.campsite_ids_by_date[d]),
            sites=build_site_views(sites_by_date.get(d, [])),
        )
        for d in sorted(availability.campsite_ids_by_date)
    )
    return CardView(
        id=card_id,
        name=name,
        # A retained snapshot remains useful for booking, but it is not current
        # availability and must sort/filter/render as a failed scan.
        state="failed" if stale_timestamp_iso else "available",
        total=availability.total_sites,
        rows=rows,
        booking_url=availability.booking_url,
        nights=build_nights_label(entry),
        # Some searches for this entry succeeded and some did not, so what is
        # shown is real but incomplete.
        partial=scan_failed,
        unique_sites=len({site.campsite_id for site in availability.campsites}),
        date_count=len(rows),
        stale_label=stale_label,
        stale_timestamp_iso=stale_timestamp_iso,
    )


def card_sort_key(card: CardView) -> tuple:
    """Order cards by how much they have to offer, most first.

    Campgrounds with availability lead, ranked by site count. Failed scans come
    next: "we could not check" is more actionable than a confirmed empty, and
    burying them under every empty campground would hide the one state the
    reader may need to do something about. Name breaks ties so the order is
    stable between scans that find the same counts.
    """
    rank = {"available": 0, "failed": 1, "empty": 2}[card.state]
    return (rank, -card.total, card.name.lower())


def build_dashboard_cards(availabilities: Iterable[ProcessedAvailability]) -> list[CardView]:
    """Build every card in display order.

    Anchor ids are assigned after sorting so they run in display order, and the
    "Jump To" nav renders from this same list, so the two always agree.
    """
    cards = sorted(
        (build_card_view(availability, "") for availability in availabilities),
        key=card_sort_key,
    )
    return [replace(card, id=f"site-{index}") for index, card in enumerate(cards)]


def build_dashboard_html(
    entries_with_results: list[tuple[dict, list[AvailableCampsite]] | ProcessedAvailability],
    day_filter: set[int] | None,
    scan_timestamp: datetime | None = None,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    search_filter: SearchFilterView | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """Generate a complete self-contained HTML string."""
    if scan_timestamp is None:
        scan_timestamp = datetime.now(timezone.utc).astimezone()
    # Provide ISO format to be parsed by JS for local timezone conversion
    if scan_timestamp.tzinfo is None:
        scan_timestamp = scan_timestamp.replace(tzinfo=timezone.utc).astimezone()

    availabilities = [
        item
        if isinstance(item, ProcessedAvailability)
        else process_results(item[0], item[1], day_filter)
        for item in entries_with_results
    ]

    cards = build_dashboard_cards(availabilities)

    all_availabilities: dict[date, int] = defaultdict(int)
    for availability in availabilities:
        # Retained last-known-good rows are shown inside their failed card but
        # never painted green in the calendar or counted as current results.
        if availability.available and availability.last_successful_at is None:
            for booking_date, campsite_ids in availability.campsite_ids_by_date.items():
                all_availabilities[booking_date] += len(campsite_ids)

    stats = None
    if cards:
        stats = StatsView(
            available=sum(card.state == "available" for card in cards),
            total=len(cards),
            soonest=min(all_availabilities).strftime("%a, %b %-d") if all_availabilities else "",
            failed=sum(card.state == "failed" or card.partial for card in cards),
        )

    calendar_start = search_filter.start if search_filter is not None else None
    calendar_end = search_filter.end if search_filter is not None else None
    calendar_day_filter = search_filter.day_filter if search_filter is not None else None
    refresh_label = ""
    if refresh_seconds > 0:
        if refresh_seconds % 60 == 0:
            minutes = refresh_seconds // 60
            refresh_label = f"Auto-refreshes every {minutes} minute{'s' if minutes != 1 else ''}"
        else:
            refresh_label = f"Auto-refreshes every {refresh_seconds} seconds"

    return (
        template_environment()
        .get_template(PAGE_TEMPLATE)
        .render(
            css=read_asset("dashboard.css"),
            js=read_asset("dashboard.js"),
            refresh_seconds=refresh_seconds,
            refresh_label=refresh_label,
            stale_after_seconds=stale_after_seconds,
            timestamp_iso=scan_timestamp.isoformat(),
            timestamp_label=scan_timestamp.strftime("%b %-d, %Y at %-I:%M %p"),
            stats=stats,
            search_filter=search_filter,
            months=build_calendar_months(
                all_availabilities,
                calendar_start,
                calendar_end,
                calendar_day_filter,
            ),
            cards=cards,
            available_cards=[card for card in cards if card.state == "available"],
            failed_cards=[card for card in cards if card.state == "failed"],
            empty_cards=[card for card in cards if card.state == "empty"],
        )
    )


def write_dashboard(html_content: str, output_path: str) -> None:
    """Write HTML content to the specified file path."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)


def generate_dashboard(
    entries_with_results: list[tuple[dict, list[AvailableCampsite]] | ProcessedAvailability],
    day_filter: set[int] | None,
    output_path: str,
    search_filter: SearchFilterView | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """Build HTML, write to disk, and immediately free the string."""
    content = build_dashboard_html(
        entries_with_results,
        day_filter,
        search_filter=search_filter,
        stale_after_seconds=stale_after_seconds,
    )
    write_dashboard(content, output_path)
    del content
    return output_path
