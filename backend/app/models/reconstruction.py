"""``reconstructions`` y ``reconstruction_inputs`` — trabajos y procedencia.

Regla dura 4 de ``CLAUDE.md``: **cada foto que entra en una reconstrucción deja fila
en ``reconstruction_inputs`` con su peso y la licencia vigente en ese momento**, y
esa fila no se borra nunca.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.domain.licensing import LicenseCode
from app.models.enums import JobStatus, job_status_enum, license_code_enum

if TYPE_CHECKING:
    from app.models.photo import Photo

__all__ = ["Reconstruction", "ReconstructionInput"]


class Reconstruction(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "reconstructions"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sky_objects.id", ondelete="SET NULL")
    )
    #: ``classical-stack-v1``, ``drizzle-v1``, ``burst-sr-v1``…
    pipeline: Mapped[str] = mapped_column(Text, nullable=False)
    #: git sha del código de ``models/`` que corrió (reproducibilidad bit a bit).
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"))
    #: Parámetros efectivos (drizzle pixfrac, kernel, rechazo…).
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    status: Mapped[JobStatus] = mapped_column(
        job_status_enum,
        nullable=False,
        server_default=text("'queued'::job_status"),
        default=JobStatus.QUEUED,
    )
    #: Progreso 0–1 que consume el SSE de ``/reconstructions/{id}/events``.
    progress: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    batch_job_id: Mapped[str | None] = mapped_column(Text)
    input_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    s3_key_result: Mapped[str | None] = mapped_column(Text)
    s3_key_preview: Mapped[str | None] = mapped_column(Text)
    s3_key_report: Mapped[str | None] = mapped_column(Text)
    #: ``ATTRIBUTION.md``; regla 5 de ``docs/licensing.md``, siempre presente.
    s3_key_attribution: Mapped[str | None] = mapped_column(Text)
    #: ``provenance.json`` firmado.
    s3_key_provenance: Mapped[str | None] = mapped_column(Text)
    #: Mapa de incertidumbre por píxel. Regla dura 2 de ``CLAUDE.md``: nada generado
    #: sin etiquetar, y en astronomía eso significa publicar la incertidumbre.
    s3_key_uncertainty: Mapped[str | None] = mapped_column(Text)
    #: Mapa de peso (cuántos frames y con qué peso contribuyeron a cada píxel).
    s3_key_weight_map: Mapped[str | None] = mapped_column(Text)
    #: ``fwhm_arcsec``, ``snr_gain_db``, ``effective_pixel_scale``, ``psnr``, ``ssim``.
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: La combinación más restrictiva de las entradas (``domain.licensing``).
    license: Mapped[LicenseCode | None] = mapped_column(license_code_enum)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compute_seconds: Mapped[float | None] = mapped_column(Float)
    cost_usd_estimate: Mapped[float | None] = mapped_column(Float)
    #: Cabecera ``Idempotency-Key`` del POST que lo creó (regla dura 3).
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )

    inputs: Mapped[list[ReconstructionInput]] = relationship(
        back_populates="reconstruction", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("requested_by", "idempotency_key", name="uq_reconstructions_idempotency"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="progress_range"),
        Index("ix_reconstructions_object_status", "object_id", "status"),
        Index("ix_reconstructions_requested_by", "requested_by", "created_at"),
        Index(
            "ix_reconstructions_queued",
            "requested_by",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


class ReconstructionInput(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "reconstruction_inputs"

    reconstruction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reconstructions.id", ondelete="CASCADE"), nullable=False
    )
    #: RESTRICT: la procedencia sobrevive al borrado lógico de la foto.
    photo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("photos.id", ondelete="RESTRICT"), nullable=False
    )
    #: Contribución efectiva 0–1.
    weight: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    was_rejected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    alignment_rms_px: Mapped[float | None] = mapped_column(Float)
    #: La licencia de la foto **en el momento** de usarla. Un cambio posterior nunca
    #: reescribe la historia de una reconstrucción ya publicada.
    snapshot_license: Mapped[LicenseCode] = mapped_column(license_code_enum, nullable=False)
    #: Cómo pidió el autor ser citado, congelado igual que la licencia.
    snapshot_attribution_name: Mapped[str | None] = mapped_column(Text)

    reconstruction: Mapped[Reconstruction] = relationship(back_populates="inputs", lazy="raise")
    photo: Mapped[Photo] = relationship(back_populates="reconstruction_inputs", lazy="raise")

    __table_args__ = (
        UniqueConstraint("reconstruction_id", "photo_id", name="uq_reconstruction_inputs_pair"),
        CheckConstraint("weight >= 0 AND weight <= 1", name="weight_range"),
        Index("ix_reconstruction_inputs_photo", "photo_id"),
        Index("ix_reconstruction_inputs_recon", "reconstruction_id", "was_rejected"),
    )
