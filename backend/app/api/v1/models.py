"""``/models`` 🔓 (lectura) y ``POST /models/{id}/activate`` (solo admin)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import SettingsDep, get_model_repository, get_storage, require_db_role
from app.core.errors import NotFoundError
from app.core.security import Role
from app.models.ml import MLModel
from app.repositories.ml import ModelRepository
from app.schemas.common import Page
from app.schemas.ml import ModelDetailOut, ModelOut
from app.services.storage import StorageService

router = APIRouter(prefix="/models", tags=["models"])

RepoDep = Annotated[ModelRepository, Depends(get_model_repository)]


def _to_out(model: MLModel) -> ModelOut:
    return ModelOut(
        id=model.id,
        name=model.name,
        version=model.version,
        architecture=model.architecture,
        is_active=model.is_active,
        trained_on_photo_count=model.trained_on_photo_count,
        metrics=model.metrics,
        respects_ai_optout=model.respects_ai_optout,
        created_at=model.created_at,
    )


@router.get("", response_model=Page[ModelOut], summary="Modelos publicados")
async def list_models(
    repo: RepoDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[ModelOut]:
    page = await repo.list_models(limit=min(limit, settings.max_page_size), cursor=cursor)
    return Page[ModelOut](items=[_to_out(m) for m in page.items], next_cursor=page.next_cursor)


@router.get(
    "/{model_id}",
    response_model=ModelDetailOut,
    summary="Model card, métricas y dataset snapshot",
)
async def read_model(
    model_id: UUID,
    repo: RepoDep,
    settings: SettingsDep,
    storage: Annotated[StorageService, Depends(get_storage)],
) -> ModelDetailOut:
    model = await repo.get(model_id)
    if model is None:
        raise NotFoundError("El modelo no existe.")

    snapshot_id = None
    photo_count = None
    if model.training_run_id:
        run = await repo.training_run(model.training_run_id)
        if run is not None and run.dataset_snapshot_id:
            snapshot_id = run.dataset_snapshot_id
            snapshot = await repo.snapshot(snapshot_id)
            photo_count = snapshot.photo_count if snapshot else None

    weights_url, _ = await storage.presigned_get(
        bucket=settings.s3_bucket_derived, key=model.s3_key_weights
    )
    base = _to_out(model)
    return ModelDetailOut(
        **base.model_dump(),
        card_markdown=model.card_markdown,
        training_run_id=model.training_run_id,
        dataset_snapshot_id=snapshot_id,
        dataset_photo_count=photo_count,
        weights_url=weights_url,
    )


@router.post(
    "/{model_id}/activate",
    response_model=ModelOut,
    summary="Activa un modelo (solo admin)",
    dependencies=[require_db_role(Role.ADMIN)],
)
async def activate_model(model_id: UUID, repo: RepoDep) -> ModelOut:
    """Solo un modelo activo por arquitectura: si hubiera dos, el pipeline elegiría
    de forma no determinista y se rompería la reproducibilidad bit a bit."""
    model = await repo.activate(model_id)
    if model is None:
        raise NotFoundError("El modelo no existe.")
    return _to_out(model)
