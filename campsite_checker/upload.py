import os
import sys
from typing import Optional


def get_r2_config(args, config: dict) -> Optional[dict]:
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


def upload_to_r2(file_path: str, r2_config: dict) -> Optional[str]:
    """Upload a file to Cloudflare R2.

    Returns the public URL if custom_domain is configured, otherwise None.
    Returns None on failure (logged as warning).
    """
    try:
        import boto3
    except ImportError:
        print(
            "  [WARNING] boto3 is required for R2 upload: pip install boto3",
            file=sys.stderr,
        )
        return None

    endpoint_url = f"https://{r2_config['account_id']}.r2.cloudflarestorage.com"

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=r2_config["access_key_id"],
            aws_secret_access_key=r2_config["secret_access_key"],
            region_name="auto",
        )
        s3.upload_file(
            file_path,
            r2_config["bucket_name"],
            r2_config["object_key"],
            ExtraArgs={
                "ContentType": "text/html; charset=utf-8",
                "CacheControl": "public, max-age=60",
            },
        )

        if r2_config.get("custom_domain"):
            domain = r2_config["custom_domain"].rstrip("/")
            return f"{domain}/{r2_config['object_key']}"
        return None

    except Exception as exc:
        print(f"  [WARNING] R2 upload failed: {exc}", file=sys.stderr)
        return None
