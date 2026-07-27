"""Weekday name <-> `datetime.weekday()` integer mappings."""

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

WEEKDAY_LABELS = {value: name.capitalize() for name, value in WEEKDAY_NAMES.items()}
