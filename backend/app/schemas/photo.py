"""Schemas de fotos: subida en 3 pasos, lectura, edición y descarga.

La ofuscación de ubicación se aplica en :meth:`PhotoOut.from_model`, que es el único
camino por el que una foto sale de este backend.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.licensing import LicenseCode
from app.domain.location import GeoPoint, LocationPrecision, obfuscate_location
from app.models.enums import LocationSource, PhotoStatus, TimeSource
from app.schemas.common import LocationIn, LocationOut, Schema

__all__ = [
    "ALLOWED_MIME_TYPES",
    "AstrometryOut",
    "DownloadOut",
    "EquipmentIn",
    "MultipartCompleteIn",
    "MultipartCompletedOut",
    "MultipartPartIn",
    "MultipartPartOut",
    "MultipartUploadOut",
    "PhotoCompleteIn",
    "PhotoOut",
    "PhotoSummaryOut",
    "PhotoUpdateIn",
    "PresignedUploadOut",
    "QualityOut",
    "UploadRequestIn",
    "UploadTicketOut",
]

#: Tipos aceptados en la subida. FITS y RAW entran; el worker decide qué puede leer.
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "image/fits",
        "application/fits",
        "image/x-canon-cr2",
        "image/x-canon-cr3",
        "image/x-nikon-nef",
        "image/x-sony-arw",
        "image/x-adobe-dng",
        "image/x-fuji-raf",
        "image/x-panasonic-rw2",
    }
)


# --------------------------------------------------------------------------- #
# Paso 1: POST /photos/uploads
# --------------------------------------------------------------------------- #
class UploadRequestIn(Schema):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: str
    #: SHA-256 en hexadecimal (64 caracteres). Sirve para deduplicar antes de subir.
    checksum_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("mime_type")
    @classmethod
    def _known_mime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_MIME_TYPES:
            raise ValueError(
                f"Tipo no soportado: {value}. Aceptados: " + ", ".join(sorted(ALLOWED_MIME_TYPES))
            )
        return normalized

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("El nombre de fichero no puede contener rutas.")
        return value

    @field_validator("checksum_sha256")
    @classmethod
    def _lower_checksum(cls, value: str) -> str:
        return value.lower()


class PresignedUploadOut(Schema):
    """POST presignado de S3 (no PUT: permite ``content-length-range`` y tags)."""

    photo_id: UUID
    upload_url: str
    fields: dict[str, str]
    expires_at: datetime
    #: Clave de destino, informativa: el cliente la manda dentro de ``fields``.
    s3_key: str
    max_bytes: int


class MultipartPartOut(Schema):
    part_number: int = Field(ge=1, le=10_000)
    url: str
    #: Bytes que debe contener esta parte (la última puede ser menor).
    size_bytes: int


class MultipartUploadOut(Schema):
    """Alternativa para ficheros > 100 MB (``docs/api.md``)."""

    photo_id: UUID
    s3_key: str
    upload_id: str
    part_urls: list[MultipartPartOut]
    expires_at: datetime
    part_size_bytes: int


class MultipartPartIn(Schema):
    """Una parte ya subida, tal como la devolvió S3."""

    part_number: int = Field(ge=1, le=10_000)
    #: ETag que devolvió el `PUT` de la parte. S3 lo entrecomilla; se acepta con o sin.
    etag: str = Field(min_length=1, max_length=200)

    @field_validator("etag")
    @classmethod
    def _normalize_etag(cls, value: str) -> str:
        return f'"{value.strip(chr(34))}"'


class MultipartCompleteIn(Schema):
    """Cuerpo de ``POST /photos/{id}/uploads/complete-multipart``."""

    upload_id: str = Field(min_length=1, max_length=500)
    parts: list[MultipartPartIn] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _parts_are_complete_and_consecutive(self) -> MultipartCompleteIn:
        """S3 exige todas las partes, numeradas desde 1 y sin huecos.

        Se valida aquí y no en S3 para poder devolver un 422 que diga *qué* falta,
        en vez del ``InvalidPart`` opaco de AWS.
        """
        numbers = [p.part_number for p in self.parts]
        if len(set(numbers)) != len(numbers):
            duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
            raise ValueError(f"Hay part_number repetidos: {duplicated}.")
        expected = list(range(1, len(numbers) + 1))
        if sorted(numbers) != expected:
            missing = sorted(set(expected) - set(numbers))
            raise ValueError(
                "Las partes deben ir numeradas desde 1 y sin huecos; "
                f"faltan {missing or 'ninguna, pero hay números fuera de rango'}."
            )
        return self


class MultipartCompletedOut(Schema):
    """Resultado del cierre: la foto ya está lista para el paso 3 (`/complete`)."""

    photo_id: UUID
    s3_key: str
    total_bytes: int
    status: PhotoStatus
    #: Recordatorio explícito de cuál es el siguiente paso del flujo de 3 pasos.
    next_step: str = "POST /photos/{photo_id}/complete"


class UploadTicketOut(Schema):
    """Respuesta de ``POST /photos/uploads``: o presignado simple, o multipart."""

    photo_id: UUID
    presigned_post: PresignedUploadOut | None = None
    multipart: MultipartUploadOut | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> UploadTicketOut:
        if (self.presigned_post is None) == (self.multipart is None):
            raise ValueError("Debe venir exactamente uno de presigned_post o multipart.")
        return self


# --------------------------------------------------------------------------- #
# Paso 3: POST /photos/{id}/complete
# --------------------------------------------------------------------------- #
class EquipmentIn(Schema):
    """Óptica y cámara declaradas por el usuario. Ganan al EXIF (``docs/api.md``)."""

    camera_make: str | None = None
    camera_model: str | None = None
    sensor_width_mm: float | None = Field(default=None, gt=0)
    sensor_height_mm: float | None = Field(default=None, gt=0)
    pixel_pitch_um: float | None = Field(default=None, gt=0)
    lens_model: str | None = None
    focal_length_mm: float | None = Field(default=None, gt=0)
    focal_ratio: float | None = Field(default=None, gt=0)
    exposure_seconds: float | None = Field(default=None, gt=0)
    iso: int | None = Field(default=None, gt=0)
    is_stacked: bool | None = None
    sub_frames: int | None = Field(default=None, ge=1)
    telescope_model: str | None = None
    mount_model: str | None = None
    is_tracked: bool | None = None
    filter_name: str | None = None

    @model_validator(mode="after")
    def _stacked_needs_subframes(self) -> EquipmentIn:
        if self.is_stacked and self.sub_frames is None:
            raise ValueError("Si is_stacked es true hay que indicar sub_frames.")
        return self


class PhotoCompleteIn(Schema):
    """Cuerpo de ``POST /photos/{id}/complete``."""

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=8000)
    license: LicenseCode | None = None
    captured_at_local: datetime | None = None
    utc_offset_minutes: int | None = Field(default=None, ge=-840, le=840)
    location: LocationIn | None = None
    location_precision: LocationPrecision = LocationPrecision.EXACT
    object_id: UUID | None = None
    site_id: UUID | None = None
    equipment: EquipmentIn | None = None
    allow_ai_training: bool = True
    allow_derivatives_in_stacks: bool = True
    attribution_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _local_time_needs_offset(self) -> PhotoCompleteIn:
        if self.captured_at_local is not None and self.utc_offset_minutes is None:
            raise ValueError(
                "captured_at_local exige utc_offset_minutes: sin offset no se puede "
                "calcular el instante UTC, y de él dependen airmass y Luna."
            )
        return self


class PhotoUpdateIn(Schema):
    """``PATCH /photos/{id}``. La licencia solo puede relajarse si está congelada."""

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=8000)
    license: LicenseCode | None = None
    object_id: UUID | None = None
    site_id: UUID | None = None
    location_precision: LocationPrecision | None = None
    allow_ai_training: bool | None = None
    allow_derivatives_in_stacks: bool | None = None
    attribution_name: str | None = Field(default=None, max_length=200)
    equipment: EquipmentIn | None = None


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #
class AstrometryOut(Schema):
    is_plate_solved: bool
    ra_deg: float | None = None
    dec_deg: float | None = None
    field_radius_deg: float | None = None
    pixel_scale_arcsec: float | None = None
    orientation_deg: float | None = None
    parity: int | None = None


class QualityOut(Schema):
    fwhm_arcsec: float | None = None
    star_count: int | None = None
    eccentricity: float | None = None
    snr_estimate: float | None = None
    background_adu: float | None = None
    bortle_estimate: int | None = None
    moon_illumination: float | None = None
    moon_separation_deg: float | None = None
    airmass: float | None = None
    quality_score: float | None = None


class OpticsOut(Schema):
    camera_make: str | None = None
    camera_model: str | None = None
    lens_model: str | None = None
    telescope_model: str | None = None
    mount_model: str | None = None
    focal_length_mm: float | None = None
    focal_ratio: float | None = None
    aperture_mm: float | None = None
    pixel_pitch_um: float | None = None
    exposure_seconds: float | None = None
    iso: int | None = None
    is_stacked: bool = False
    sub_frames: int | None = None
    is_tracked: bool | None = None
    filter_name: str | None = None
    #: Derivado: límite de difracción de esta óptica, arcsec. Techo duro de detalle.
    diffraction_limit_arcsec: float | None = None


class LicenseOut(Schema):
    code: LicenseCode
    locked_at: datetime | None = None
    attribution_name: str | None = None
    allow_ai_training: bool = True
    allow_derivatives_in_stacks: bool = True


class PhotoSummaryOut(Schema):
    """Versión corta para listados y búsqueda."""

    id: UUID
    owner_id: UUID
    status: PhotoStatus
    title: str | None = None
    object_id: UUID | None = None
    thumb_url: str | None = None
    preview_url: str | None = None
    captured_at_utc: datetime | None = None
    quality_score: float | None = None
    license: LicenseCode
    location: LocationOut | None = None
    created_at: datetime


class PhotoOut(Schema):
    """``GET /photos/{id}`` — metadata completa con la ubicación ya ofuscada."""

    id: UUID
    owner_id: UUID
    status: PhotoStatus
    title: str | None = None
    description: str | None = None
    object_id: UUID | None = None
    site_id: UUID | None = None

    mime_type: str | None = None
    original_bytes: int | None = None
    width_px: int | None = None
    height_px: int | None = None
    bit_depth: int | None = None
    checksum_sha256: str | None = None
    preview_url: str | None = None
    thumb_url: str | None = None

    captured_at_utc: datetime | None = None
    captured_at_local: datetime | None = None
    utc_offset_minutes: int | None = None
    time_source: TimeSource | None = None

    location: LocationOut | None = None
    location_source: LocationSource | None = None

    optics: OpticsOut
    astrometry: AstrometryOut
    quality: QualityOut
    license: LicenseOut

    view_count: int = 0
    download_count: int = 0
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def obfuscated_location(photo: Any, *, viewer_is_owner: bool = False) -> LocationOut | None:
        """Aplica la privacidad del autor. **Único** punto por el que sale una ubicación.

        El propietario ve siempre su posición exacta: la privacidad protege de
        terceros, no del autor.

        ``lat_deg``/``lon_deg`` los rellena el repositorio con
        ``ST_Y(location::geometry)`` / ``ST_X(location::geometry)``; el modelo ORM
        guarda un ``WKBElement`` que aquí no se decodifica. Si faltan, no se publica
        ubicación: preferimos un ``null`` a una coordenada dudosa.
        """
        lat = getattr(photo, "lat_deg", None)
        lon = getattr(photo, "lon_deg", None)
        if lat is None or lon is None:
            return None
        point = GeoPoint(
            lat=float(lat),
            lon=float(lon),
            accuracy_m=photo.location_accuracy_m,
            elevation_m=photo.elevation_m,
            country_code=photo.country_code,
        )
        precision = LocationPrecision.EXACT if viewer_is_owner else photo.location_precision
        return LocationOut.from_domain(obfuscate_location(point, precision))


class DownloadOut(Schema):
    """``GET /photos/{id}/download`` cuando el cliente pide JSON en vez del 302."""

    url: str
    expires_at: datetime
    license: LicenseCode
    attribution: str
