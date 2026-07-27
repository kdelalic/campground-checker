"""ReserveCalifornia (UseDirect) provider hardening.

Stock camply UseDirect calls carry no HTTP timeout and cache their offline
metadata inside site-packages; both are corrected here.
"""

import logging
import os
import pathlib

from camply.providers.base_provider import ProviderError
from camply.providers.usedirect.variations import ReserveCalifornia
from camply.search import SearchReserveCalifornia

logger = logging.getLogger(__name__)

# Applied to providers whose stock camply HTTP calls carry no timeout; a
# black-holed connection would otherwise hang a scan thread forever.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30

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
    """

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
        response = self.session.request(
            method=method,
            url=url,
            data=data,
            headers=headers,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code not in retry_response_codes:
            response.raise_for_status()
        else:
            error_message = (
                f"HTTP Error - {self.__class__.__name__} - {response.url} - {response.status_code}"
            )
            logger.warning(error_message)
            error_message += f": {response.text}"
            raise ProviderError(error_message)
        return response


class TimeoutSearchReserveCalifornia(SearchReserveCalifornia):
    """SearchReserveCalifornia wired to the timeout-enforcing provider."""

    provider_class = TimeoutReserveCalifornia
