"""Provider registry: maps config `provider` strings to camply search classes.

Each submodule holds the hardening for one provider family; this module only
wires the names together. To add a provider, add its search class here and put
any subclassing it needs in a sibling module.
"""

from camply.search import (
    SearchGoingToCamp,
    SearchYellowstone,
)

from .recreation_gov import (
    IdentityCachedRecreationDotGov,
    NativeSearchRecreationDotGov,
)
from .reserve_america import NativeSearchReserveAmerica
from .reserve_california import (
    NativeSearchReserveCalifornia,
    TimeoutReserveCalifornia,
)

__all__ = [
    "METADATA_PROVIDER_CLASS",
    "PROVIDER_DISPLAY",
    "PROVIDER_MAP",
]

PROVIDER_MAP: dict[str, type] = {
    "RecreationDotGov": NativeSearchRecreationDotGov,
    "Yellowstone": SearchYellowstone,
    "GoingToCamp": SearchGoingToCamp,
    "ReserveCalifornia": NativeSearchReserveCalifornia,
    "ReserveAmerica": NativeSearchReserveAmerica,
}

# Provider classes used for out-of-search metadata lookups (bot name resolution),
# sharing the same caching/timeout hardening as the search path.
METADATA_PROVIDER_CLASS: dict[str, type] = {
    "RecreationDotGov": IdentityCachedRecreationDotGov,
    "ReserveCalifornia": TimeoutReserveCalifornia,
}

PROVIDER_DISPLAY: dict[str, str] = {
    "RecreationDotGov": "recreation.gov",
    "Yellowstone": "yellowstone",
    "GoingToCamp": "goingtocamping.com",
    "ReserveCalifornia": "reservecalifornia",
    "ReserveAmerica": "reserveamerica",
}
