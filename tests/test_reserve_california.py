"""Tests for the ReserveCalifornia (UseDirect) provider hardening."""

import pathlib
from types import SimpleNamespace

import camply

from campsite_checker.providers.reserve_california import (
    CAMPLY_CACHE_DIR_ENV,
    DEFAULT_CAMPLY_CACHE_DIR,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    TimeoutReserveCalifornia,
)


class TestTimeoutReserveCalifornia:
    def test_make_http_request_passes_timeout(self):
        """Stock camply UseDirect requests hang forever without a timeout."""
        captured = {}

        def fake_request(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                url=kwargs["url"],
                text="",
            )

        fake_self = SimpleNamespace(
            session=SimpleNamespace(request=fake_request),
            FIVE_HUNDRED_STATUS_CODES=[500, 502, 503],
        )
        response = TimeoutReserveCalifornia.make_http_request(fake_self, "https://example.com/api")

        assert response.status_code == 200
        assert captured["timeout"] == DEFAULT_HTTP_TIMEOUT_SECONDS

    def test_offline_cache_dir_defaults_outside_install_tree(self, monkeypatch):
        """Camply defaults the UseDirect cache to site-packages, which the
        unprivileged container user cannot write; the override must land
        somewhere else."""
        monkeypatch.delenv(CAMPLY_CACHE_DIR_ENV, raising=False)
        cache_dir = TimeoutReserveCalifornia().offline_cache_dir
        assert cache_dir == DEFAULT_CAMPLY_CACHE_DIR / "reserve-california"
        camply_install_tree = pathlib.Path(camply.__file__).parent
        assert camply_install_tree not in cache_dir.resolve().parents

    def test_offline_cache_dir_honors_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(CAMPLY_CACHE_DIR_ENV, str(tmp_path / "camply-cache"))
        cache_dir = TimeoutReserveCalifornia().offline_cache_dir
        assert cache_dir == tmp_path / "camply-cache" / "reserve-california"
