"""Schemas de ``/models`` 🔓."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.models.enums import ModelArchitecture
from app.schemas.common import Schema

__all__ = ["ModelDetailOut", "ModelOut"]


class ModelOut(Schema):
    id: UUID
    name: str
    version: str
    architecture: ModelArchitecture
    is_active: bool
    trained_on_photo_count: int | None = None
    metrics: dict[str, Any] | None = None
    #: Siempre true en modelos publicados; se expone para poder auditarlo.
    respects_ai_optout: bool = True
    created_at: datetime


class ModelDetailOut(ModelOut):
    """``GET /models/{id}`` — model card, métricas y snapshot de entrenamiento."""

    card_markdown: str | None = None
    training_run_id: UUID | None = None
    dataset_snapshot_id: UUID | None = None
    dataset_photo_count: int | None = None
    weights_url: str | None = None
