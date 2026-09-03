"""Acceso directo a MinIO, para comprobar lo que el backend dice que guardó.

Los tests no se creen la respuesta de la API: van al bucket y miran.
"""

from __future__ import annotations

import functools
import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

__all__ = ["bucket_uploads", "get_object", "head_object", "s3_client"]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@functools.lru_cache(maxsize=1)
def s3_client() -> Any:
    """Cliente apuntando al MinIO de `docker-compose.dev.yml`."""
    return boto3.client(
        "s3",
        endpoint_url=_env("S3_ENDPOINT_URL", "http://localhost:9000"),
        region_name=_env("S3_REGION", "eu-west-1"),
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def bucket_uploads() -> str:
    return _env("S3_BUCKET_UPLOADS", "astro-photos-dev-uploads")


def get_object(key: str, bucket: str | None = None) -> bytes | None:
    """Bytes del objeto, o `None` si no existe."""
    try:
        resp = s3_client().get_object(Bucket=bucket or bucket_uploads(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return bytes(resp["Body"].read())


def head_object(key: str, bucket: str | None = None) -> dict[str, Any] | None:
    try:
        return dict(s3_client().head_object(Bucket=bucket or bucket_uploads(), Key=key))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "403"):
            return None
        raise
