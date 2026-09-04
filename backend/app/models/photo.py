"""``photos`` — la entidad central. Una fila = una exposición subida por un usuario.

Sigue campo a campo ``docs/data-model.md``. Los bloques y su orden son los del
documento para que la comparación sea trivial en revisión.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    cast,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.domain.licensing import LicenseCode
from app.domain.location import LocationPrecision
from app.models.enums import (
    LocationSource,
    PhotoStatus,
    TimeSource,
    license_code_enum,
    location_precision_enum,
    location_source_enum,
    photo_status_enum,
    time_source_enum,
)

if TYPE_CHECKING:
    from app.models.reconstruction import ReconstructionInput
    from app.models.site import ObservingSite
    from app.models.sky_object import SkyObject
    from app.models.user import User

__all__ = ["EMBEDDING_DIM", "SKY_LON_SHIFT", "Photo", "sky_geography"]

#: Desplazamiento que lleva RA ∈ [0, 360) al rango de longitudes [-180, 180).
#: Es una rotación, así que conserva las separaciones angulares.
SKY_LON_SHIFT = 180.0


def sky_geography(ra_deg: Any, dec_deg: Any) -> Any:
    """La posición en el cielo como ``geography(Point,4326)``.

    Tratar (RA, Dec) como (longitud, latitud) sobre una esfera hace que la distancia
    entre dos puntos **sea** su separación angular, sin necesidad de ninguna
    extensión de esferas. Y usar ``geography`` en vez de aritmética sobre las dos
    columnas hace que PostGIS se ocupe de la topología: el corte en RA = 0/360 y la
    degeneración de la RA cerca de los polos dejan de ser casos especiales.

    **Esta función es la única definición de la expresión.** El índice GIST de
    ``photos`` se construye sobre ella y la consulta del cono la reutiliza; si las
    dos se escribieran por separado, un cambio en una dejaría a la otra sin índice
    de forma silenciosa. El test de plan (`EXPLAIN` sin `Seq Scan`) es lo que
    detecta esa deriva si alguna vez ocurre.
    """
    return cast(
        func.ST_SetSRID(func.ST_MakePoint(ra_deg - SKY_LON_SHIFT, dec_deg), 4326),
        Geography(geometry_type="POINT", srid=4326),
    )


#: Dimensión del embedding visual (``vector(768)`` en ``docs/data-model.md``).
EMBEDDING_DIM = 768


class Photo(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "photos"

    #: ON DELETE RESTRICT: no se borran fotos con derechos cedidos.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[PhotoStatus] = mapped_column(
        photo_status_enum,
        nullable=False,
        server_default=text("'uploading'::photo_status"),
        default=PhotoStatus.UPLOADING,
    )
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sky_objects.id", ondelete="SET NULL")
    )

    # --- Almacenamiento ---------------------------------------------------- #
    s3_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    #: Original inmutable; nunca se sirve directo.
    s3_key_original: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key_preview: Mapped[str | None] = mapped_column(Text)
    s3_key_thumb: Mapped[str | None] = mapped_column(Text)
    original_bytes: Mapped[int | None] = mapped_column(BigInteger)
    #: Deduplicación; UNIQUE por ``owner_id``.
    checksum_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: `UploadId` de S3 mientras una subida multipart está abierta; NULL en cuanto se
    #: cierra o se aborta. Se guarda para poder validar que el `upload_id` que manda
    #: el cliente en `complete-multipart` es realmente el de esta foto.
    multipart_upload_id: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(SmallInteger)

    # --- Tiempo ------------------------------------------------------------- #
    captured_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Hora de pared del observador (sin tz a propósito).
    captured_at_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    utc_offset_minutes: Mapped[int | None] = mapped_column(SmallInteger)
    time_source: Mapped[TimeSource | None] = mapped_column(time_source_enum)

    # --- Lugar -------------------------------------------------------------- #
    location: Mapped[Any | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    location_accuracy_m: Mapped[float | None] = mapped_column(Float)
    elevation_m: Mapped[float | None] = mapped_column(Float)
    location_source: Mapped[LocationSource | None] = mapped_column(location_source_enum)
    #: Privacidad del autor. La ofuscación se aplica en el serializador.
    location_precision: Mapped[LocationPrecision] = mapped_column(
        location_precision_enum,
        nullable=False,
        server_default=text("'exact'::location_precision"),
        default=LocationPrecision.EXACT,
    )
    #: ISO 3166-1 alfa-2, resuelto por el worker; lo usa la precisión ``country``.
    country_code: Mapped[str | None] = mapped_column(Text)
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("observing_sites.id", ondelete="SET NULL")
    )

    # --- Óptica y cámara ----------------------------------------------------- #
    camera_make: Mapped[str | None] = mapped_column(Text)
    camera_model: Mapped[str | None] = mapped_column(Text)
    sensor_width_mm: Mapped[float | None] = mapped_column(Float)
    sensor_height_mm: Mapped[float | None] = mapped_column(Float)
    #: Derivado; clave para el muestreo sub-píxel.
    pixel_pitch_um: Mapped[float | None] = mapped_column(Float)
    lens_model: Mapped[str | None] = mapped_column(Text)
    focal_length_mm: Mapped[float | None] = mapped_column(Float)
    focal_ratio: Mapped[float | None] = mapped_column(Float)
    #: Derivado ``focal_length_mm / focal_ratio``; fija el límite de difracción.
    aperture_mm: Mapped[float | None] = mapped_column(Float)
    exposure_seconds: Mapped[float | None] = mapped_column(Float)
    iso: Mapped[int | None] = mapped_column(Integer)
    is_stacked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    sub_frames: Mapped[int | None] = mapped_column(Integer)
    telescope_model: Mapped[str | None] = mapped_column(Text)
    mount_model: Mapped[str | None] = mapped_column(Text)
    is_tracked: Mapped[bool | None] = mapped_column(Boolean)
    filter_name: Mapped[str | None] = mapped_column(Text)

    # --- Astrometría (worker de plate solving) -------------------------------- #
    is_plate_solved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    ra_deg: Mapped[float | None] = mapped_column(Float(precision=53))
    dec_deg: Mapped[float | None] = mapped_column(Float(precision=53))
    field_radius_deg: Mapped[float | None] = mapped_column(Float)
    pixel_scale_arcsec: Mapped[float | None] = mapped_column(Float)
    orientation_deg: Mapped[float | None] = mapped_column(Float)
    parity: Mapped[int | None] = mapped_column(SmallInteger)
    #: WCS completo (CTYPE/CRVAL/CRPIX/CD) para reproyección.
    wcs_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    astrometry_job_id: Mapped[str | None] = mapped_column(Text)
    #: Fase sub-píxel derivada del WCS; la consume ``domain.selection``.
    dither_phase_x: Mapped[float | None] = mapped_column(Float)
    dither_phase_y: Mapped[float | None] = mapped_column(Float)

    # --- Calidad (worker de QA de imagen) ------------------------------------- #
    fwhm_arcsec: Mapped[float | None] = mapped_column(Float)
    star_count: Mapped[int | None] = mapped_column(Integer)
    eccentricity: Mapped[float | None] = mapped_column(Float)
    background_adu: Mapped[float | None] = mapped_column(Float)
    snr_estimate: Mapped[float | None] = mapped_column(Float)
    bortle_estimate: Mapped[int | None] = mapped_column(SmallInteger)
    moon_illumination: Mapped[float | None] = mapped_column(Float)
    moon_separation_deg: Mapped[float | None] = mapped_column(Float)
    airmass: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)

    # --- Licencia -------------------------------------------------------------- #
    license: Mapped[LicenseCode] = mapped_column(
        license_code_enum,
        nullable=False,
        server_default=text(f"'{LicenseCode.CC_BY_NC.value}'::license_code"),
        default=LicenseCode.CC_BY_NC,
    )
    #: Tras la primera descarga por terceros la licencia deja de poder endurecerse.
    license_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attribution_name: Mapped[str | None] = mapped_column(Text)
    allow_ai_training: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    allow_derivatives_in_stacks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )

    # --- Otros ----------------------------------------------------------------- #
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    view_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    download_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    #: EXIF/XMP crudo tal cual, para poder reprocesar sin volver a bajar el original.
    exif_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Soft-delete: ``DELETE /photos/{id}`` no borra filas (procedencia perpetua).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship(back_populates="photos", lazy="raise")
    sky_object: Mapped[SkyObject | None] = relationship(lazy="raise")
    site: Mapped[ObservingSite | None] = relationship(lazy="raise")
    reconstruction_inputs: Mapped[list[ReconstructionInput]] = relationship(
        back_populates="photo", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "checksum_sha256", name="uq_photos_owner_checksum"),
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)",
            name="quality_score_range",
        ),
        CheckConstraint(
            "bortle_estimate IS NULL OR (bortle_estimate BETWEEN 1 AND 9)",
            name="bortle_range",
        ),
        CheckConstraint("dec_deg IS NULL OR (dec_deg BETWEEN -90 AND 90)", name="dec_range"),
        CheckConstraint("ra_deg IS NULL OR (ra_deg >= 0 AND ra_deg < 360)", name="ra_range"),
        # GIST sobre location: consultas "a menos de N km de aquí".
        Index("ix_photos_location_gist", "location", postgresql_using="gist"),
        Index("ix_photos_object_captured", "object_id", "captured_at_utc"),
        # BTREE sobre (ra_deg, dec_deg). No puede acelerar una búsqueda por cono
        # —ninguna función de distancia esférica es indexable por BTREE— pero sirve
        # para búsquedas por coordenada exacta. Candidato a retirar en una migración
        # `contract` aparte si se confirma que nadie la usa.
        Index("ix_photos_radec", "ra_deg", "dec_deg"),
        # El índice que sostiene la búsqueda por cono, que es la consulta central
        # del producto. Parcial porque solo las fotos resueltas tienen coordenadas.
        Index(
            "ix_photos_sky_gist",
            sky_geography(column("ra_deg"), column("dec_deg")),
            postgresql_using="gist",
            postgresql_where=text("ra_deg IS NOT NULL AND dec_deg IS NOT NULL"),
        ),
        # HNSW sobre el embedding para GET /photos/similar/{id}.
        Index(
            "ix_photos_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Parcial: la búsqueda pública solo mira fotos listas y resueltas.
        Index(
            "ix_photos_ready_solved",
            "object_id",
            "quality_score",
            postgresql_where=text("status = 'ready' AND is_plate_solved"),
        ),
        Index("ix_photos_owner_status", "owner_id", "status"),
        # El barrido de subidas grandes abandonadas busca justo estas filas.
        Index(
            "ix_photos_open_multipart",
            "owner_id",
            postgresql_where=text("multipart_upload_id IS NOT NULL"),
        ),
        Index("ix_photos_captured_at", "captured_at_utc"),
    )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<Photo {self.id} {self.status}>"
