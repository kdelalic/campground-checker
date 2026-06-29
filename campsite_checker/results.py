from collections import defaultdict
from datetime import date, datetime, timedelta

from camply.containers import AvailableCampsite


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


def group_results(
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> tuple[str, dict[date, set], int, str] | None:
    """Filter results and group by date.

    Returns None if no results remain after filtering, otherwise returns
    (facility_name, by_date, total_sites, booking_url).
    """
    filtered = filter_results(results, day_filter)
    if not filtered:
        return None
    name = get_facility_name(filtered)
    by_date: dict[date, set] = defaultdict(set)
    for r in filtered:
        by_date[r.booking_date.date()].add(r.campsite_id)
    total = sum(len(s) for s in by_date.values())
    url = get_booking_url(filtered)
    return name, by_date, total, url


def format_results(
    entry: dict,
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> str | None:
    """Returns formatted string if there are results, None otherwise."""
    grouped = group_results(results, day_filter)
    if grouped is None:
        return None
    name, by_date, total, url = grouped

    lines = [f"\n**{name}** — {total} open site(s)"]
    for d in sorted(by_date):
        count = len(by_date[d])
        lines.append(f"  \U0001f4c5 {d.strftime('%a, %b %-d')}: {count} site(s)")
    if url:
        lines.append(f"  \U0001f517 {url}")
    return "\n".join(lines)
