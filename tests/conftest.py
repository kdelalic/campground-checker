"""Shared test fixtures and helpers."""

from datetime import datetime
from types import SimpleNamespace


def make_campsite(
    campsite_id=1,
    facility_id=100,
    facility_name="Test Campground",
    recreation_area="Test Area",
    booking_date=None,
    booking_nights=1,
    campsite_type="STANDARD",
    campsite_site_name="A1",
    campsite_loop_name="Loop A",
    booking_url="https://example.com/camp",
    campsite_attributes=None,
    latitude=None,
    longitude=None,
):
    """Create a mock AvailableCampsite-like object for testing.

    Uses SimpleNamespace so all attribute accesses via getattr work
    the same as a real AvailableCampsite dataclass.
    """
    if booking_date is None:
        booking_date = datetime(2026, 7, 4)
    if campsite_attributes is None:
        campsite_attributes = [
            SimpleNamespace(attribute_name="Site Access", attribute_value="Drive-in"),
        ]
    location = None
    if latitude is not None and longitude is not None:
        location = SimpleNamespace(latitude=latitude, longitude=longitude)
    return SimpleNamespace(
        campsite_id=campsite_id,
        facility_id=facility_id,
        facility_name=facility_name,
        recreation_area=recreation_area,
        booking_date=booking_date,
        booking_nights=booking_nights,
        campsite_type=campsite_type,
        campsite_site_name=campsite_site_name,
        campsite_loop_name=campsite_loop_name,
        booking_url=booking_url,
        campsite_attributes=campsite_attributes,
        location=location,
    )
