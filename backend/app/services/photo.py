"""Lectura, edición, borrado y descarga de fotos."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog

from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, UnprocessableError
from app.domain.astro import diffraction_limit_arcsec
from app.domain.licensing import can_change_license, enforce_stack_consent
from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.photo import PhotoRepository
from app.repositories.reconstruction import ReconstructionRepository
from app.schemas.common import LocationOut
from app.schemas.photo import (
    AstrometryOut,
    LicenseOut,
    OpticsOut,
    PhotoOut,
    PhotoSummaryOut,
    PhotoUpdateIn,
    QualityOut,
)
from app.services.storage import StorageService

__all__ = ["PhotoService"]

log = structlog.get_logger(__name__)


class PhotoService:
    def __init__(
        self,
        *,
        photos: PhotoRepository,
        reconstructions: ReconstructionRepository,
        audit: AuditRepository,
        storage: StorageService,
        settings: Settings,
    ) -> None:
        self.photos = photos
        self.reconstructions = reconstructions
        self.audit = audit
        self.storage = storage
        self.settings = settings

    # ------------------------------------------------------------------ #
    # Serialización (aquí y solo aquí se ofusca la ubicación)
    # ------------------------------------------------------------------ #
    async def _signed(self, key: str | None) -> str | None:
        if not key:
            return None
        url, _ = await self.storage.presigned_get(bucket=self.settings.s3_bucket_derived, key=key)
        return url

    async def to_out(self, photo: Photo, *, viewer: User | None) -> PhotoOut:
        """Convierte una foto a su representación pública.

        **Único** camino por el que una foto sale del backend, y por tanto el único
        sitio donde se decide qué ubicación se publica.
        """
        is_owner = viewer is not None and viewer.id == photo.owner_id
        return PhotoOut(
            id=photo.id,
            owner_id=photo.owner_id,
            status=photo.status,
            title=photo.title,
            description=photo.description,
            object_id=photo.object_id,
            site_id=photo.site_id,
            mime_type=photo.mime_type,
            original_bytes=photo.original_bytes,
            width_px=photo.width_px,
            height_px=photo.height_px,
            bit_depth=photo.bit_depth,
            checksum_sha256=photo.checksum_sha256.hex() if photo.checksum_sha256 else None,
            preview_url=await self._signed(photo.s3_key_preview),
            thumb_url=await self._signed(photo.s3_key_thumb),
            captured_at_utc=photo.captured_at_utc,
            captured_at_local=photo.captured_at_local,
            utc_offset_minutes=photo.utc_offset_minutes,
            time_source=photo.time_source,
            location=PhotoOut.obfuscated_location(photo, viewer_is_owner=is_owner),
            location_source=photo.location_source,
            optics=OpticsOut(
                camera_make=photo.camera_make,
                camera_model=photo.camera_model,
                lens_model=photo.lens_model,
                telescope_model=photo.telescope_model,
                mount_model=photo.mount_model,
                focal_length_mm=photo.focal_length_mm,
                focal_ratio=photo.focal_ratio,
                aperture_mm=photo.aperture_mm,
                pixel_pitch_um=photo.pixel_pitch_um,
                exposure_seconds=photo.exposure_seconds,
                iso=photo.iso,
                is_stacked=photo.is_stacked,
                sub_frames=photo.sub_frames,
                is_tracked=photo.is_tracked,
                filter_name=photo.filter_name,
                diffraction_limit_arcsec=(
                    diffraction_limit_arcsec(photo.aperture_mm)
                    if photo.aperture_mm and photo.aperture_mm > 0
                    else None
                ),
            ),
            astrometry=AstrometryOut(
                is_plate_solved=photo.is_plate_solved,
                ra_deg=photo.ra_deg,
                dec_deg=photo.dec_deg,
                field_radius_deg=photo.field_radius_deg,
                pixel_scale_arcsec=photo.pixel_scale_arcsec,
                orientation_deg=photo.orientation_deg,
                parity=photo.parity,
            ),
            quality=QualityOut(
                fwhm_arcsec=photo.fwhm_arcsec,
                star_count=photo.star_count,
                eccentricity=photo.eccentricity,
                snr_estimate=photo.snr_estimate,
                background_adu=photo.background_adu,
                bortle_estimate=photo.bortle_estimate,
                moon_illumination=photo.moon_illumination,
                moon_separation_deg=photo.moon_separation_deg,
                airmass=photo.airmass,
                quality_score=photo.quality_score,
            ),
            license=LicenseOut(
                code=photo.license,
                locked_at=photo.license_locked_at,
                attribution_name=photo.attribution_name,
                allow_ai_training=photo.allow_ai_training,
                allow_derivatives_in_stacks=photo.allow_derivatives_in_stacks,
            ),
            view_count=photo.view_count,
            download_count=photo.download_count,
            created_at=photo.created_at,
            updated_at=photo.updated_at,
        )

    async def to_summary(self, photo: Photo, *, viewer: User | None) -> PhotoSummaryOut:
        is_owner = viewer is not None and viewer.id == photo.owner_id
        location: LocationOut | None = PhotoOut.obfuscated_location(photo, viewer_is_owner=is_owner)
        return PhotoSummaryOut(
            id=photo.id,
            owner_id=photo.owner_id,
            status=photo.status,
            title=photo.title,
            object_id=photo.object_id,
            thumb_url=await self._signed(photo.s3_key_thumb),
            preview_url=await self._signed(photo.s3_key_preview),
            captured_at_utc=photo.captured_at_utc,
            quality_score=photo.quality_score,
            license=photo.license,
            location=location,
            created_at=photo.created_at,
        )

    # ------------------------------------------------------------------ #
    async def get_visible(self, photo_id: uuid.UUID, *, viewer: User | None) -> Photo:
        """Una foto es visible si está lista, o si quien mira es su dueño."""
        photo = await self.photos.get(photo_id)
        if photo is None:
            raise NotFoundError("La foto no existe.")
        is_owner = viewer is not None and viewer.id == photo.owner_id
        if photo.status is not PhotoStatus.READY and not is_owner:
            raise NotFoundError("La foto no existe.")
        if photo.status is PhotoStatus.QUARANTINED and not is_owner:
            raise NotFoundError("La foto no existe.")
        return photo

    async def update(self, *, photo_id: uuid.UUID, user: User, payload: PhotoUpdateIn) -> Photo:
        """``PATCH /photos/{id}``. La licencia pasa por ``domain.licensing``."""
        photo = await self.photos.get(photo_id)
        if photo is None:
            raise NotFoundError("La foto no existe.")
        if photo.owner_id != user.id:
            raise ForbiddenError("Solo el autor puede editar esta foto.")

        if payload.license is not None and payload.license != photo.license:
            decision = can_change_license(photo.license, payload.license, photo.license_locked_at)
            if not decision.allowed:
                raise UnprocessableError(
                    decision.reason,
                    extra={
                        "current_license": photo.license.value,
                        "requested_license": payload.license.value,
                        "license_locked_at": (
                            photo.license_locked_at.isoformat() if photo.license_locked_at else None
                        ),
                    },
                )
            await self.audit.record(
                action="photo.license_changed",
                entity_type="photo",
                entity_id=photo.id,
                actor_id=user.id,
                payload={"from": photo.license.value, "to": payload.license.value},
            )
            photo.license = payload.license

        for field in ("title", "description", "object_id", "site_id", "attribution_name"):
            value = getattr(payload, field)
            if value is not None:
                setattr(photo, field, value)
        if payload.location_precision is not None:
            photo.location_precision = payload.location_precision
        if payload.allow_ai_training is not None:
            photo.allow_ai_training = payload.allow_ai_training
        if payload.allow_derivatives_in_stacks is not None:
            photo.allow_derivatives_in_stacks = payload.allow_derivatives_in_stacks
        if payload.equipment is not None:
            eq = payload.equipment
            for field in (
                "camera_make",
                "camera_model",
                "lens_model",
                "focal_length_mm",
                "focal_ratio",
                "exposure_seconds",
                "iso",
                "telescope_model",
                "mount_model",
                "is_tracked",
                "filter_name",
                "pixel_pitch_um",
            ):
                value = getattr(eq, field)
                if value is not None:
                    setattr(photo, field, value)
            if photo.focal_length_mm and photo.focal_ratio:
                photo.aperture_mm = photo.focal_length_mm / photo.focal_ratio

        # Coherencia ND ⇒ sin derivadas en stacks, siempre, venga de donde venga.
        photo.allow_derivatives_in_stacks = enforce_stack_consent(
            photo.license, photo.allow_derivatives_in_stacks
        )
        return photo

    async def soft_delete(self, *, photo_id: uuid.UUID, user: User) -> None:
        """``DELETE /photos/{id}`` — soft-delete; 409 si está en algo publicado."""
        photo = await self.photos.get(photo_id)
        if photo is None:
            raise NotFoundError("La foto no existe.")
        if photo.owner_id != user.id:
            raise ForbiddenError("Solo el autor puede borrar esta foto.")
        if await self.reconstructions.photo_is_in_published(photo_id):
            raise ConflictError(
                "Esta foto participa en una reconstrucción publicada. La procedencia "
                "es perpetua, así que no puede retirarse; sí puedes cambiar su "
                "visibilidad y su consentimiento para usos futuros."
            )
        photo.deleted_at = datetime.now(UTC)
        await self.audit.record(
            action="photo.deleted",
            entity_type="photo",
            entity_id=photo.id,
            actor_id=user.id,
        )

    async def download(
        self, *, photo_id: uuid.UUID, viewer: User | None
    ) -> tuple[str, datetime, Photo]:
        """URL firmada del original. Incrementa ``download_count`` y audita.

        La primera descarga por un tercero **congela la licencia**: a partir de ahí
        solo puede relajarse (``docs/licensing.md``).
        """
        photo = await self.get_visible(photo_id, viewer=viewer)
        url, expires = await self.storage.presigned_get(
            bucket=photo.s3_bucket,
            key=photo.s3_key_original,
            filename=f"{photo.id}",
        )
        await self.photos.increment_download(photo.id)
        is_third_party = viewer is None or viewer.id != photo.owner_id
        if is_third_party and photo.license_locked_at is None:
            photo.license_locked_at = datetime.now(UTC)
            log.info("license_locked", photo_id=str(photo.id))
        await self.audit.record(
            action="photo.downloaded",
            entity_type="photo",
            entity_id=photo.id,
            actor_id=viewer.id if viewer else None,
            payload={"license": photo.license.value},
        )
        return url, expires, photo

    @staticmethod
    def attribution_line(photo: Photo, author_name: str) -> str:
        """Línea de crédito lista para pegar. Regla 5: atribución siempre."""
        title = photo.title or f"Foto {photo.id}"
        name = photo.attribution_name or author_name
        return f'"{title}" — {name} ({photo.license.value})'
