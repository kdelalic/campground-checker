import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class R2UploadResult:
    success: bool
    public_url: str | None = None


class R2Uploader:
    """Reusable Cloudflare R2 uploader with a persistent boto3 client."""

    def __init__(self, r2_config: dict, client: Any = None):
        self.r2_config = r2_config
        self._client = client
        self._client_initialized = client is not None

    def _get_client(self):
        if self._client_initialized:
            return self._client
        self._client_initialized = True
        try:
            import boto3
        except ImportError:
            logger.warning("boto3 is required for R2 upload: pip install boto3")
            return None

        endpoint_url = f"https://{self.r2_config['account_id']}.r2.cloudflarestorage.com"
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.r2_config["access_key_id"],
            aws_secret_access_key=self.r2_config["secret_access_key"],
            region_name="auto",
        )
        return self._client

    def upload(self, file_path: str) -> R2UploadResult:
        """Upload a dashboard while retaining the client's connection pool."""
        try:
            client = self._get_client()
            if client is None:
                return R2UploadResult(success=False)
            client.upload_file(
                file_path,
                self.r2_config["bucket_name"],
                self.r2_config["object_key"],
                ExtraArgs={
                    "ContentType": "text/html; charset=utf-8",
                    "CacheControl": "public, max-age=60",
                },
            )
        except Exception as exc:
            logger.warning("R2 upload failed: %s", exc)
            return R2UploadResult(success=False)

        public_url = None
        if self.r2_config.get("custom_domain"):
            domain = self.r2_config["custom_domain"].rstrip("/")
            public_url = f"{domain}/{self.r2_config['object_key']}"
        return R2UploadResult(success=True, public_url=public_url)


def get_r2_config(args, config: dict) -> dict | None:
    """Resolve R2 credentials and config.

    Priority for each field: CLI args > env vars > YAML config.
    Returns None if R2 is not configured (missing required credentials).
    """
    r2_cfg = (config.get("dashboard") or {}).get("r2") or {}

    account_id = os.environ.get("R2_ACCOUNT_ID") or r2_cfg.get("account_id")
    access_key_id = os.environ.get("R2_ACCESS_KEY_ID") or r2_cfg.get("access_key_id")
    secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY") or r2_cfg.get("secret_access_key")
    bucket_name = (
        getattr(args, "r2_bucket", None)
        or os.environ.get("R2_BUCKET_NAME")
        or r2_cfg.get("bucket_name")
    )
    object_key = os.environ.get("R2_OBJECT_KEY") or r2_cfg.get("object_key", "index.html")
    custom_domain = os.environ.get("R2_CUSTOM_DOMAIN") or r2_cfg.get("custom_domain")

    if not all([account_id, access_key_id, secret_access_key, bucket_name]):
        return None

    return {
        "account_id": account_id,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket_name": bucket_name,
        "object_key": object_key,
        "custom_domain": custom_domain,
    }


def upload_to_r2(file_path: str, r2_config: dict) -> str | None:
    """Upload a file to Cloudflare R2.

    Returns the public URL if custom_domain is configured, otherwise None.
    Returns None on failure (logged as warning).
    """
    result = R2Uploader(r2_config).upload(file_path)
    return result.public_url if result.success else None
