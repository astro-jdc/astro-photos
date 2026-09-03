"""Subida en 3 pasos. El binario nunca pasa por aquí: solo se firman URLs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError, QuotaExceededError
from app.domain.licensing import enforce_stack_consent
from app.models.enums import LocationSource, PhotoStatus, TimeSource
from app.models.photo import Photo
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.photo import PhotoRepository
from app.repositories.user import UserRepository
from app.schemas.photo import (
    MultipartPartOut,
    MultipartUploadOut,
    PhotoCompleteIn,
    PresignedUploadOut,
    UploadRequestIn,
    UploadTicketOut,
)
from app.services.queue import QueueService
from app.services.storage import StorageService

__all__ = ["UploadService"]

log = structlog.get_logger(__name__)


class UploadService:
    """Orquesta ``POST /photos/uploads`` y ``POST /photos/{id}/complete``."""

    def __init__(
        self,
        *,
        photos: PhotoRepository,
        users: UserRepository,
        audit: AuditRepository,
        storage: StorageService,
        queue: QueueService,
        settings: Settings,
    ) -> None:
        self.photos = photos
        self.users = users
        self.audit = audit
        self.storage = storage
        self.queue = queue
        self.settings = settings

    # ------------------------------------------------------------------ #
    async def create_upload(self, *, user: User, request: UploadRequestIn) -> UploadTicketOut:
        """Paso 1: valida cuota, tipo y duplicado; devuelve el presignado.

        Se crea ya la fila ``photos`` en estado ``uploading``: sin ella no habría a
        qué asociar el objeto de S3 si el cliente desaparece a mitad, y el barrido
        de huérfanos no sabría qué limpiar.
        """
        if request.size_bytes > self.settings.max_upload_bytes:
            raise BadRequestError(
                f"El fichero pesa {request.size_bytes} bytes y el máximo por fichero "
                f"es {self.settings.max_upload_bytes}.",
                errors=[{"pointer": "/size_bytes", "detail": "supera el máximo"}],
            )

        available = user.storage_quota_bytes - user.storage_used_bytes
        if request.size_bytes > available:
            raise QuotaExceededError(
                f"Te quedan {available} bytes de cuota y el fichero ocupa "
                f"{request.size_bytes}. Libera espacio o pide ampliación.",
                extra={
                    "quota_bytes": user.storage_quota_bytes,
                    "used_bytes": user.storage_used_bytes,
                    "required_bytes": request.size_bytes,
                },
            )

        checksum = bytes.fromhex(request.checksum_sha256)
        existing = await self.photos.find_by_checksum(user.id, checksum)
        if existing is not None:
            raise ConflictError(
                "Ya has subido esta misma imagen (mismo SHA-256).",
                extra={"photo_id": str(existing.id), "checksum_sha256": request.checksum_sha256},
            )

        photo_id = uuid.uuid4()
        key = self.storage.staging_key(str(user.id), str(photo_id), request.filename)
        photo = Photo(
            id=photo_id,
            owner_id=user.id,
            status=PhotoStatus.UPLOADING,
            s3_bucket=self.settings.s3_bucket_uploads,
            s3_key_original=key,
            original_bytes=request.size_bytes,
            checksum_sha256=checksum,
            mime_type=request.mime_type,
            license=user.default_license,
            attribution_name=user.attribution_name or user.display_name,
        )
        await self.photos.add(photo)
        await self.audit.record(
            action="photo.upload_requested",
            entity_type="photo",
            entity_id=photo_id,
            actor_id=user.id,
            payload={"size_bytes": request.size_bytes, "mime_type": request.mime_type},
        )

        if request.size_bytes > self.settings.multipart_threshold_bytes:
            multipart = await self.storage.create_multipart_upload(
                key=key, size_bytes=request.size_bytes, content_type=request.mime_type
            )
            return UploadTicketOut(
                photo_id=photo_id,
                multipart=MultipartUploadOut(
                    photo_id=photo_id,
                    s3_key=multipart.key,
                    upload_id=multipart.upload_id,
                    part_urls=[
                        MultipartPartOut(part_number=n, url=u, size_bytes=s)
                        for n, u, s in multipart.part_urls
                    ],
                    expires_at=multipart.expires_at,
                    part_size_bytes=multipart.part_size_bytes,
                ),
            )

        post = await self.storage.create_presigned_post(
            key=key,
            max_bytes=request.size_bytes,
            min_bytes=request.size_bytes,
            content_type=request.mime_type,
            tags={"photo_id": str(photo_id), "owner": str(user.id)},
        )
        return UploadTicketOut(
            photo_id=photo_id,
            presigned_post=PresignedUploadOut(
                photo_id=photo_id,
                upload_url=post.url,
                fields=post.fields,
                expires_at=post.expires_at,
                s3_key=post.key,
                max_bytes=post.max_bytes,
            ),
        )

    # ------------------------------------------------------------------ #
    async def complete_upload(
        self, *, user: User, photo_id: uuid.UUID, payload: PhotoCompleteIn
    ) -> Photo:
        """Paso 3: fija la metadata declarada, marca ``processing`` y encola ingesta.

        Los campos que el cliente manda **ganan** al EXIF y quedan marcados con
        ``*_source='user'`` (``docs/api.md``); los que no manda los rellena el worker.
        """
        photo = await self.photos.get(photo_id)
        if photo is None:
            raise NotFoundError("La foto no existe.")
        if photo.owner_id != user.id:
            raise NotFoundError("La foto no existe.")
        if photo.status is not PhotoStatus.UPLOADING:
            raise ConflictError(
                f"La foto ya está en estado «{photo.status.value}»; `complete` solo "
                "se puede llamar una vez."
            )

        head = await self.storage.head(bucket=photo.s3_bucket, key=photo.s3_key_original)
        if head is None:
            raise ConflictError(
                "El fichero todavía no está en S3. Sube el binario a la URL "
                "presignada antes de llamar a `complete`."
            )
        actual_bytes = int(head.get("ContentLength", 0))
        if photo.original_bytes and actual_bytes != photo.original_bytes:
            raise ConflictError(
                f"El objeto subido pesa {actual_bytes} bytes y se anunciaron "
                f"{photo.original_bytes}."
            )

        if payload.title is not None:
            photo.title = payload.title
        if payload.description is not None:
            photo.description = payload.description
        if payload.license is not None:
            photo.license = payload.license
        if payload.attribution_name is not None:
            photo.attribution_name = payload.attribution_name
        photo.object_id = payload.object_id or photo.object_id
        photo.site_id = payload.site_id or photo.site_id

        if payload.captured_at_local is not None and payload.utc_offset_minutes is not None:
            photo.captured_at_local = payload.captured_at_local
            photo.utc_offset_minutes = payload.utc_offset_minutes
            photo.captured_at_utc = payload.captured_at_local.replace(tzinfo=UTC) - timedelta(
                minutes=payload.utc_offset_minutes
            )
            photo.time_source = TimeSource.USER

        if payload.location is not None:
            photo.location = f"SRID=4326;POINT({payload.location.lon} {payload.location.lat})"
            photo.location_accuracy_m = payload.location.accuracy_m
            photo.elevation_m = payload.location.elevation_m
            photo.location_source = LocationSource.USER_PIN
        photo.location_precision = payload.location_precision

        if payload.equipment is not None:
            eq = payload.equipment
            for field in (
                "camera_make",
                "camera_model",
                "sensor_width_mm",
                "sensor_height_mm",
                "pixel_pitch_um",
                "lens_model",
                "focal_length_mm",
                "focal_ratio",
                "exposure_seconds",
                "iso",
                "sub_frames",
                "telescope_model",
                "mount_model",
                "is_tracked",
                "filter_name",
            ):
                value = getattr(eq, field)
                if value is not None:
                    setattr(photo, field, value)
            if eq.is_stacked is not None:
                photo.is_stacked = eq.is_stacked
            if photo.focal_length_mm and photo.focal_ratio:
                photo.aperture_mm = photo.focal_length_mm / photo.focal_ratio

        photo.allow_ai_training = payload.allow_ai_training
        # Un ND/ARR fuerza el opt-out; la coherencia se aplica al escribir.
        photo.allow_derivatives_in_stacks = enforce_stack_consent(
            photo.license, payload.allow_derivatives_in_stacks
        )
        photo.status = PhotoStatus.PROCESSING

        await self.users.reserve_quota(user.id, actual_bytes)
        await self.audit.record(
            action="photo.completed",
            entity_type="photo",
            entity_id=photo.id,
            actor_id=user.id,
            payload={
                "license": photo.license.value,
                "location_precision": photo.location_precision.value,
                "allow_ai_training": photo.allow_ai_training,
                "allow_derivatives_in_stacks": photo.allow_derivatives_in_stacks,
            },
        )
        await self.queue.send(
            self.settings.sqs_queue_ingest,
            {
                "type": "ingest",
                "photo_id": str(photo.id),
                "owner_id": str(user.id),
                "bucket": photo.s3_bucket,
                "key": photo.s3_key_original,
                "enqueued_at": datetime.now(UTC).isoformat(),
            },
            idempotency_key=f"ingest:{photo.id}",
        )
        log.info("photo_ingest_enqueued", photo_id=str(photo.id))
        return photo
