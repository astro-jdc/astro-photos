"""S3 / MinIO. **Ningún binario pasa por el backend** (regla dura 6 de ``CLAUDE.md``).

Todo lo que hace este servicio es firmar URLs. boto3 es síncrono, así que cada
llamada se despacha a un hilo con ``asyncio.to_thread``: un ``generate_presigned_*``
hace criptografía local y no red, pero el ``head_object`` sí, y bloquear el event
loop en un handler está prohibido (regla dura 8).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, get_settings
from app.core.errors import UpstreamError

__all__ = ["MultipartUpload", "PresignedPost", "StorageService"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PresignedPost:
    url: str
    fields: dict[str, str]
    key: str
    expires_at: datetime
    max_bytes: int


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    key: str
    upload_id: str
    part_urls: list[tuple[int, str, int]]
    expires_at: datetime
    part_size_bytes: int


class StorageService:
    """Fachada sobre S3. Una instancia por proceso; el cliente boto3 es thread-safe."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=self._cfg.s3_endpoint_url,
            region_name=self._cfg.s3_region,
            aws_access_key_id=self._cfg.aws_access_key_id,
            aws_secret_access_key=self._cfg.aws_secret_access_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                # MinIO no soporta virtual-host addressing sin DNS comodín.
                s3={"addressing_style": "path" if self._cfg.s3_endpoint_url else "auto"},
            ),
        )

    # ------------------------------------------------------------------ #
    def staging_key(self, user_id: str, photo_id: str, filename: str) -> str:
        """``staging/{user}/{photo_id}/{filename}`` (``docs/api.md``)."""
        safe = filename.replace("/", "_").replace("\\", "_")
        return f"staging/{user_id}/{photo_id}/{safe}"

    def original_key(self, user_id: str, photo_id: str) -> str:
        return f"originals/{user_id}/{photo_id}"

    def derived_key(self, photo_id: str, kind: str, ext: str) -> str:
        return f"derived/{photo_id}/{kind}.{ext}"

    # ------------------------------------------------------------------ #
    async def create_presigned_post(
        self,
        *,
        key: str,
        max_bytes: int,
        content_type: str,
        min_bytes: int = 1,
        tags: dict[str, str] | None = None,
    ) -> PresignedPost:
        """POST presignado con ``content-length-range``.

        Se usa POST y no PUT justamente por esto: el cliente no puede subir un
        fichero de otro tamaño ni de otro tipo del que declaró, y la política la
        firma el servidor (``docs/api.md``).
        """
        expires = self._cfg.upload_url_ttl_seconds
        conditions: list[Any] = [
            ["content-length-range", min_bytes, max_bytes],
            {"Content-Type": content_type},
        ]
        fields: dict[str, str] = {"Content-Type": content_type}
        if tags:
            tagging = "&".join(f"{k}={v}" for k, v in sorted(tags.items()))
            fields["x-amz-tagging"] = tagging
            conditions.append({"x-amz-tagging": tagging})

        try:
            result = await asyncio.to_thread(
                self._client.generate_presigned_post,
                Bucket=self._cfg.s3_bucket_uploads,
                Key=key,
                Fields=fields,
                Conditions=conditions,
                ExpiresIn=expires,
            )
        except (BotoCoreError, ClientError) as exc:
            log.error("presign_post_failed", key=key, error=str(exc))
            raise UpstreamError("No se pudo preparar la subida a S3.") from exc

        return PresignedPost(
            url=str(result["url"]),
            fields={str(k): str(v) for k, v in result["fields"].items()},
            key=key,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires),
            max_bytes=max_bytes,
        )

    async def create_multipart_upload(
        self, *, key: str, size_bytes: int, content_type: str
    ) -> MultipartUpload:
        """Multipart para ficheros grandes (> 100 MB, ``docs/api.md``)."""
        chunk = self._cfg.multipart_chunk_bytes
        expires = self._cfg.upload_url_ttl_seconds
        try:
            created = await asyncio.to_thread(
                self._client.create_multipart_upload,
                Bucket=self._cfg.s3_bucket_uploads,
                Key=key,
                ContentType=content_type,
            )
            upload_id = str(created["UploadId"])
            part_count = max(1, -(-size_bytes // chunk))  # ceil
            parts: list[tuple[int, str, int]] = []
            for number in range(1, part_count + 1):
                url = await asyncio.to_thread(
                    self._client.generate_presigned_url,
                    "upload_part",
                    Params={
                        "Bucket": self._cfg.s3_bucket_uploads,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": number,
                    },
                    ExpiresIn=expires,
                )
                remaining = size_bytes - (number - 1) * chunk
                parts.append((number, str(url), min(chunk, remaining)))
        except (BotoCoreError, ClientError) as exc:
            log.error("multipart_failed", key=key, error=str(exc))
            raise UpstreamError("No se pudo preparar la subida multipart.") from exc

        return MultipartUpload(
            key=key,
            upload_id=upload_id,
            part_urls=parts,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires),
            part_size_bytes=chunk,
        )

    async def presigned_get(
        self, *, bucket: str, key: str, filename: str | None = None
    ) -> tuple[str, datetime]:
        """URL firmada de descarga. En AWS la firma CloudFront; en local, S3."""
        ttl = self._cfg.download_url_ttl_seconds
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        try:
            url = await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            log.error("presign_get_failed", key=key, error=str(exc))
            raise UpstreamError("No se pudo firmar la descarga.") from exc
        return str(url), datetime.now(UTC) + timedelta(seconds=ttl)

    async def head(self, *, bucket: str, key: str) -> dict[str, Any] | None:
        """Metadata del objeto, o ``None`` si no existe."""
        try:
            return dict(await asyncio.to_thread(self._client.head_object, Bucket=bucket, Key=key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "403"):
                return None
            raise UpstreamError("S3 no respondió correctamente.") from exc
        except BotoCoreError as exc:
            raise UpstreamError("S3 no respondió correctamente.") from exc

    async def healthy(self) -> bool:
        """``/readyz``: el bucket de subidas responde."""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._cfg.s3_bucket_uploads)
        except (BotoCoreError, ClientError):
            return False
        return True
