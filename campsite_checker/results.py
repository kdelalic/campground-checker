from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from camply.containers import AvailableCampsite

NotificationKey = tuple[str, int | str, date]


@dataclass(frozen=True, slots=True)
class ProcessedAvailability:
    """Normalized availability data shared by every output consumer."""

    entry: dict
    campsites: tuple[AvailableCampsite, ...]
    facility_name: str
    booking_url: str
    campsite_ids_by_date: dict[date, frozenset[int | str]]
    notification_keys: frozenset[NotificationKey]
    total_sites: int

    @property
    def available(self) -> bool:
        return bool(self.campsites)


def get_facility_name(results: list[AvailableCampsite]) -> str:
    """Extract recreation area + facility name from the first result that has one."""
    for r in results:
        facility = getattr(r, "facility_name", "") or ""
        rec_area = getattr(r, "recreation_area", "") or ""
        if rec_area and facility:
            return f"{rec_area} — {facility}"
        if rec_area:
            return rec_area
        if facility:
            return facility
    return "Unknown"


def get_booking_url(results: list[AvailableCampsite]) -> str:
    """Extract booking URL from the first result that has one."""
    for r in results:
        url = getattr(r, "booking_url", "")
        if url:
            return url.replace("Web/Default.aspx#!", "")
    return ""


def count_matching_dates(start_dt: datetime, end_dt: datetime, day_filter: set[int] | None) -> int:
    """Count how many dates in [start_dt, end_dt) match the day filter."""
    if day_filter is None:
        return (end_dt.date() - start_dt.date()).days
    count = 0
    d = start_dt.date()
    end = end_dt.date()
    while d < end:
        if d.weekday() in day_filter:
            count += 1
        d += timedelta(days=1)
    return count


def filter_results(
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> list[AvailableCampsite]:
    """Filter out boat/hike-in sites and apply day-of-week filter."""

    def _is_excluded(r: AvailableCampsite) -> bool:
        fields = " ".join(
            [
                getattr(r, "campsite_type", "") or "",
                getattr(r, "campsite_site_name", "") or "",
                getattr(r, "campsite_loop_name", "") or "",
            ]
        ).upper()
        if any(kw in fields for kw in ("BOAT", "HIKE")):
            return True
        for attr in getattr(r, "campsite_attributes", None) or []:
            if getattr(attr, "attribute_name", "") == "Site Access" and any(
                kw in str(getattr(attr, "attribute_value", "")).upper() for kw in ("BOAT", "HIKE")
            ):
                return True
        return False

    results = [r for r in results if not _is_excluded(r)]
    if day_filter is not None:
        results = [r for r in results if r.booking_date.weekday() in day_filter]
    return results


def process_filtered_results(
    entry: dict,
    results: list[AvailableCampsite],
) -> ProcessedAvailability:
    """Deduplicate and normalize results that have already been filtered."""
    seen: set[tuple[int | str, date]] = set()
    unique: list[AvailableCampsite] = []
    for result in results:
        key = (result.campsite_id, result.booking_date.date())
        if key not in seen:
            seen.add(key)
            unique.append(result)

    name = get_facility_name(unique)
    url = get_booking_url(unique)
    by_date: dict[date, set[int | str]] = defaultdict(set)
    for result in unique:
        by_date[result.booking_date.date()].add(result.campsite_id)

    frozen_by_date = {booking_date: frozenset(ids) for booking_date, ids in by_date.items()}
    notification_keys = frozenset(
        (name, result.campsite_id, result.booking_date.date()) for result in unique
    )
    return ProcessedAvailability(
        entry=entry,
        campsites=tuple(unique),
        facility_name=name,
        booking_url=url,
        campsite_ids_by_date=frozen_by_date,
        notification_keys=notification_keys,
        total_sites=sum(len(ids) for ids in frozen_by_date.values()),
    )


def process_results(
    entry: dict,
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> ProcessedAvailability:
    """Filter, deduplicate, and normalize raw Camply results once."""
    return process_filtered_results(entry, filter_results(results, day_filter))


def group_results(
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> tuple[str, dict[date, set], int, str] | None:
    """Filter results and group by date.

    Returns None if no results remain after filtering, otherwise returns
    (facility_name, by_date, total_sites, booking_url).
    """
    processed = process_results({}, results, day_filter)
    if not processed.available:
        return None
    by_date = {
        booking_date: set(ids) for booking_date, ids in processed.campsite_ids_by_date.items()
    }
    return (
        processed.facility_name,
        by_date,
        processed.total_sites,
        processed.booking_url,
    )


def format_processed_results(processed: ProcessedAvailability) -> str | None:
    """Format normalized availability for terminal output."""
    if not processed.available:
        return None

    lines = [f"\n**{processed.facility_name}** — {processed.total_sites} open site(s)"]
    for d in sorted(processed.campsite_ids_by_date):
        count = len(processed.campsite_ids_by_date[d])
        lines.append(f"  \U0001f4c5 {d.strftime('%a, %b %-d')}: {count} site(s)")
    if processed.booking_url:
        lines.append(f"  \U0001f517 {processed.booking_url}")
    return "\n".join(lines)


def format_results(
    entry: dict,
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> str | None:
    """Filter and format raw results for backward-compatible callers."""
    return format_processed_results(process_results(entry, results, day_filter))
