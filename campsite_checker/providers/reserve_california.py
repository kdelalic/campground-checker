"""ReserveCalifornia (UseDirect) provider hardening.

Stock camply UseDirect calls carry no HTTP timeout, cache their offline
metadata inside site-packages, and issue every request at the same priority;
all three are corrected here.
"""

import logging
import os
import pathlib
from functools import lru_cache

from camply.providers.base_provider import ProviderError
from camply.providers.usedirect.variations import ReserveCalifornia
from camply.search import SearchReserveCalifornia

from ..request_gate import (
    PRIORITY_ALERT_REQUEST,
    RequestGate,
    pause_gate_on_rate_limit,
)

logger = logging.getLogger(__name__)

# Applied to providers whose stock camply HTTP calls carry no timeout; a
# black-holed connection would otherwise hang a scan thread forever.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30

# Camply searches UseDirect with a serial month x campground loop, so a
# 4-campground dashboard batch issues ~28 requests back to back. Several of
# those batches at once measurably slowed concurrent alert requests, so bound
# how many requests are in flight and let alert requests take the next start.
# There is no observed ReserveCalifornia rate limit, so starts are not spaced.
#
# One of these slots is reserved for alert requests, leaving dashboard scans
# the same 2 concurrent requests they had before the gate existed. Ordering
# alone was not enough in production: ReserveCalifornia requests take seconds,
# so an alert that waits for an in-flight dashboard request to finish still
# lost ~30s across a 7-request scan.
MAX_CONCURRENT_REQUESTS = 3

RESERVE_CALIFORNIA_REQUEST_GATE = RequestGate(max_concurrent=MAX_CONCURRENT_REQUESTS)

# Camply's UseDirect providers default their offline metadata cache to a
# directory beside the installed package, which is read-only in the container
# (the process runs as an unprivileged user). The cache must live somewhere
# writable; the container sets CAMPLY_CACHE_DIR to the persistent state mount.
CAMPLY_CACHE_DIR_ENV = "CAMPLY_CACHE_DIR"
DEFAULT_CAMPLY_CACHE_DIR = pathlib.Path(".camply-cache")


def usedirect_cache_dir(provider_slug: str) -> pathlib.Path:
    """Writable offline-cache directory for a UseDirect provider."""
    base = os.environ.get(CAMPLY_CACHE_DIR_ENV)
    root = pathlib.Path(base) if base else DEFAULT_CAMPLY_CACHE_DIR
    return root / provider_slug


class TimeoutReserveCalifornia(ReserveCalifornia):
    """ReserveCalifornia provider whose HTTP requests always carry a timeout.

    Camply's UseDirect providers call ``session.request`` without a timeout
    (unlike its Recreation.gov, GoingToCamp, and Yellowstone providers, which
    pass ``timeout=30``). Without one, a dropped connection blocks the search
    thread indefinitely and silently stalls the background dashboard worker.
    Mirrors ``BaseProvider.make_http_request``; ``make_http_request_retry``
    delegates here, so every UseDirect HTTP path gets the timeout.

    The offline metadata cache is also relocated: camply defaults it to
    ``site-packages/camply/providers/usedirect/<ClassName>``, which the
    unprivileged container user cannot create. Every metadata refresh goes
    through ``offline_cache_dir``, so overriding the property is sufficient.

    Every UseDirect HTTP path funnels through this method, so it is also where
    the shared request gate is applied.
    """

    request_gate = RESERVE_CALIFORNIA_REQUEST_GATE
    request_priority = PRIORITY_ALERT_REQUEST

    @property
    def offline_cache_dir(self) -> pathlib.Path:
        return usedirect_cache_dir("reserve-california")

    def make_http_request(
        self,
        url,
        method="GET",
        data=None,
        headers=None,
        retry_response_codes=None,
    ):
        if retry_response_codes is None:
            retry_response_codes = self.FIVE_HUNDRED_STATUS_CODES
        with self.request_gate.slot(self.request_priority):
            response = self.session.request(
                method=method,
                url=url,
                data=data,
                headers=headers,
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code not in retry_response_codes:
                try:
                    response.raise_for_status()
                except Exception as exc:
                    # Pause before releasing the slot so batches already
                    # executing stop issuing requests immediately.
                    pause_gate_on_rate_limit(self.request_gate, exc, provider="ReserveCalifornia")
                    raise
            else:
                error_message = (
                    f"HTTP Error - {self.__class__.__name__} - "
                    f"{response.url} - {response.status_code}"
                )
                logger.warning(error_message)
                error_message += f": {response.text}"
                raise ProviderError(error_message)
        return response


@lru_cache(maxsize=None)
def provider_class_for_priority(priority: int) -> type:
    """Return a provider class that issues requests at ``priority``.

    Camply builds the provider with a bare ``self.provider_class()`` inside
    ``BaseCampingSearch.__init__``, before any search argument is applied, so
    the priority has to be carried by the class itself for the identity lookups
    made during construction to be gated correctly too.
    """
    if priority == PRIORITY_ALERT_REQUEST:
        return TimeoutReserveCalifornia
    return type(
        f"{TimeoutReserveCalifornia.__name__}Priority{priority}",
        (TimeoutReserveCalifornia,),
        {"request_priority": priority},
    )


class TimeoutSearchReserveCalifornia(SearchReserveCalifornia):
    """SearchReserveCalifornia wired to the timeout-enforcing provider."""

    provider_class = TimeoutReserveCalifornia

    def __init__(
        self,
        search_window,
        recreation_area,
        *args,
        request_priority: int = PRIORITY_ALERT_REQUEST,
        **kwargs,
    ):
        # ``recreation_area`` stays an explicit, default-less parameter: it is
        # how ``search._requires_recreation_area`` knows to pass ``[]`` when a
        # config entry only names campgrounds.
        self.request_priority = request_priority
        # Shadow the class attribute before camply constructs the provider.
        self.provider_class = provider_class_for_priority(request_priority)
        super().__init__(search_window, recreation_area, *args, **kwargs)
