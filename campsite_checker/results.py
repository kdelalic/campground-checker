from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

from camply.containers import AvailableCampsite


def get_entry_url(entry: dict) -> str:
    return entry.get("url", "")


def count_matching_dates(
    start_dt: datetime, end_dt: datetime, day_filter: Optional[Set[int]]
) -> int:
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
    results: List[AvailableCampsite],
    day_filter: Optional[Set[int]],
) -> List[AvailableCampsite]:
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
                kw in str(getattr(attr, "attribute_value", "")).upper()
                for kw in ("BOAT", "HIKE")
            ):
                return True
        return False

    results = [r for r in results if not _is_excluded(r)]
    if day_filter is not None:
        results = [r for r in results if r.booking_date.weekday() in day_filter]
    return results


def format_results(
    entry: dict,
    results: List[AvailableCampsite],
    day_filter: Optional[Set[int]],
) -> Optional[str]:
    """Returns formatted string if there are results, None otherwise."""
    results = filter_results(results, day_filter)
    if not results:
        return None

    name = entry.get("name", "Unnamed")

    by_date: Dict[date, Set] = defaultdict(set)
    for r in results:
        by_date[r.booking_date.date()].add(r.campsite_id)

    total = sum(len(sites) for sites in by_date.values())
    url = get_entry_url(entry)

    lines = [f"\n**{name}** — {total} open site(s)"]
    for d in sorted(by_date):
        count = len(by_date[d])
        lines.append(f"  \U0001f4c5 {d.strftime('%a, %b %-d')}: {count} site(s)")
    lines.append(f"  \U0001f517 {url}")
    return "\n".join(lines)
