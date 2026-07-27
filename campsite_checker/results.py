import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from camply.containers import AvailableCampsite

# (provider, entry identity, campsite_id, booking_date). Keyed on stable config
# identity rather than the provider-supplied facility name, so a facility rename
# cannot re-trigger or suppress alerts.
NotificationKey = tuple[str, str, int | str, date]


def entry_identity(entry: dict, facility_name: str = "") -> str:
    """Stable identity string for an entry, preferring configured IDs."""
    cid = entry.get("campground_id")
    if cid is not None:
        if isinstance(cid, (list, tuple, set)):
            return ",".join(str(c) for c in sorted(cid, key=str))
        return str(cid)
    rec_area = entry.get("recreation_area")
    if rec_area is not None:
        if isinstance(rec_area, (list, tuple, set)):
            return "ra:" + ",".join(str(r) for r in sorted(rec_area, key=str))
        return f"ra:{rec_area}"
    return facility_name


def make_notification_key(entry: dict, facility_name: str, result) -> NotificationKey:
    """Build the dedup key for one available campsite result."""
    return (
        entry.get("provider", "RecreationDotGov"),
        entry_identity(entry, facility_name),
        result.campsite_id,
        result.booking_date.date(),
    )


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
    search_succeeded: bool = True

    @property
    def available(self) -> bool:
        return bool(self.campsites)


def availability_fingerprint(
    availabilities: list[ProcessedAvailability],
) -> str:
    """Hash dashboard-relevant state without including the generated timestamp."""
    payload = []
    for availability in availabilities:
        entry = availability.entry
        entry_identity = {
            "provider": entry.get("provider", "RecreationDotGov"),
            "campground_id": entry.get("campground_id"),
            "recreation_area": entry.get("recreation_area"),
            "campsite_id": entry.get("campsite_id"),
            "name": entry.get("name"),
        }
        dates = [
            [
                booking_date.isoformat(),
                sorted(campsite_ids, key=str),
            ]
            for booking_date, campsite_ids in sorted(availability.campsite_ids_by_date.items())
        ]
        payload.append(
            {
                "entry": entry_identity,
                "facility_name": availability.facility_name,
                "booking_url": availability.booking_url,
                "dates": dates,
            }
        )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


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
    total_days = (end_dt.date() - start_dt.date()).days
    if day_filter is None:
        return total_days

    full_weeks, remaining_days = divmod(total_days, 7)
    count = full_weeks * len(day_filter)
    start_weekday = start_dt.weekday()
    return count + sum(
        (start_weekday + offset) % 7 in day_filter for offset in range(remaining_days)
    )


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

    if day_filter is None:
        return [result for result in results if not _is_excluded(result)]

    # Apply the cheap, selective weekday check first so excluded-site metadata
    # is only inspected for dates the caller can actually use.
    return [
        result
        for result in results
        if result.booking_date.weekday() in day_filter and not _is_excluded(result)
    ]


def process_filtered_results(
    entry: dict,
    results: list[AvailableCampsite],
    *,
    search_succeeded: bool = True,
) -> ProcessedAvailability:
    """Deduplicate and normalize results that have already been filtered."""
    seen: set[tuple[int | str, date]] = set()
    unique: list[AvailableCampsite] = []
    by_date: dict[date, set[int | str]] = defaultdict(set)
    for result in results:
        booking_date = result.booking_date.date()
        key = (result.campsite_id, booking_date)
        if key not in seen:
            seen.add(key)
            unique.append(result)
            by_date[booking_date].add(result.campsite_id)

    name = get_facility_name(unique)
    url = get_booking_url(unique)

    frozen_by_date = {booking_date: frozenset(ids) for booking_date, ids in by_date.items()}
    notification_keys = frozenset(make_notification_key(entry, name, result) for result in unique)
    return ProcessedAvailability(
        entry=entry,
        campsites=tuple(unique),
        facility_name=name,
        booking_url=url,
        campsite_ids_by_date=frozen_by_date,
        notification_keys=notification_keys,
        total_sites=len(unique),
        search_succeeded=search_succeeded,
    )


def process_results(
    entry: dict,
    results: list[AvailableCampsite],
    day_filter: set[int] | None,
) -> ProcessedAvailability:
    """Filter, deduplicate, and normalize raw Camply results once."""
    return process_filtered_results(entry, filter_results(results, day_filter))


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
