from camply.search import (
    SearchGoingToCamp,
    SearchRecreationDotGov,
    SearchReserveCalifornia,
    SearchYellowstone,
)

PROVIDER_MAP: dict[str, type] = {
    "RecreationDotGov": SearchRecreationDotGov,
    "Yellowstone": SearchYellowstone,
    "GoingToCamp": SearchGoingToCamp,
    "ReserveCalifornia": SearchReserveCalifornia,
}

PROVIDER_DISPLAY: dict[str, str] = {
    "RecreationDotGov": "recreation.gov",
    "Yellowstone": "yellowstone",
    "GoingToCamp": "goingtocamping.com",
    "ReserveCalifornia": "reservecalifornia",
}

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
