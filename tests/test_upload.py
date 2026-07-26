"""Tests for reusable Cloudflare R2 uploads."""

import sys
from types import SimpleNamespace

from campsite_checker.upload import R2Uploader


def make_config():
    return {
        "account_id": "account",
        "access_key_id": "access",
        "secret_access_key": "secret",
        "bucket_name": "bucket",
        "object_key": "dashboard.html",
        "custom_domain": "https://camp.example/",
    }


def test_reuses_boto_client_across_uploads(monkeypatch):
    client = SimpleNamespace(upload_file=lambda *args, **kwargs: None)
    factory_calls = []

    def make_client(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return client

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=make_client))
    uploader = R2Uploader(make_config())

    first = uploader.upload("dashboard.html")
    second = uploader.upload("dashboard.html")

    assert len(factory_calls) == 1
    assert first.success is True
    assert second.success is True
    assert first.public_url == "https://camp.example/dashboard.html"


def test_failed_upload_is_reported():
    def fail(*args, **kwargs):
        raise RuntimeError("network unavailable")

    uploader = R2Uploader(
        make_config(),
        client=SimpleNamespace(upload_file=fail),
    )

    result = uploader.upload("dashboard.html")

    assert result.success is False
    assert result.public_url is None
