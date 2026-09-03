"""``models``, ``training_runs`` y ``dataset_snapshots``."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    JobStatus,
    ModelArchitecture,
    job_status_enum,
    model_architecture_enum,
)

__all__ = ["DatasetSnapshot", "MLModel", "TrainingRun"]


class MLModel(UUIDPkMixin, TimestampMixin, Base):
    """Pesos entrenados versionados. Los pesos van a S3, nunca a git."""

    __tablename__ = "models"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    architecture: Mapped[ModelArchitecture] = mapped_column(model_architecture_enum, nullable=False)
    s3_key_weights: Mapped[str] = mapped_column(Text, nullable=False)
    training_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("training_runs.id", ondelete="SET NULL")
    )
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    trained_on_photo_count: Mapped[int | None] = mapped_column(Integer)
    #: Model card en Markdown. Regla dura 2: nada generado sin etiquetar.
    card_markdown: Mapped[str | None] = mapped_column(Text)
    #: Siempre ``true`` en modelos publicados (``docs/data-model.md``).
    respects_ai_optout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_models_name_version"),
        Index("ix_models_active", "architecture", postgresql_where=text("is_active")),
    )


class TrainingRun(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "training_runs"

    dataset_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_snapshots.id", ondelete="SET NULL")
    )
    git_sha: Mapped[str] = mapped_column(Text, nullable=False)
    hyperparams: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[JobStatus] = mapped_column(
        job_status_enum,
        nullable=False,
        server_default=text("'queued'::job_status"),
        default=JobStatus.QUEUED,
    )
    final_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    log_s3_key: Mapped[str | None] = mapped_column(Text)
    #: ``xpu-arc-b70``, ``g5.xlarge``…
    hardware: Mapped[str | None] = mapped_column(Text)


class DatasetSnapshot(UUIDPkMixin, TimestampMixin, Base):
    """Snapshot inmutable de qué fotos formaron un conjunto de entrenamiento.

    Sirve para reproducibilidad y para poder purgar a quien revoque
    ``allow_ai_training``.
    """

    __tablename__ = "dataset_snapshots"

    photo_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(pg.UUID(as_uuid=True)),
        nullable=False,
        server_default=text("'{}'::uuid[]"),
        default=list,
    )
    filter_query: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    photo_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    __table_args__ = (UniqueConstraint("checksum", name="uq_dataset_snapshots_checksum"),)
