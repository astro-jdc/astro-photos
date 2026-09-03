"""Acceso a datos de ``models``, ``training_runs`` y ``dataset_snapshots``."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml import DatasetSnapshot, MLModel, TrainingRun
from app.repositories.base import CursorPage, KeysetCursor, parse_cursor

__all__ = ["ModelRepository"]


class ModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, model_id: uuid.UUID) -> MLModel | None:
        return await self.session.get(MLModel, model_id)

    async def list_models(self, *, limit: int, cursor: str | None) -> CursorPage[MLModel]:
        stmt = select(MLModel).order_by(MLModel.created_at.desc(), MLModel.id.desc())
        parsed = parse_cursor(cursor)
        if parsed is not None:
            stmt = stmt.where(MLModel.id < uuid.UUID(parsed.last_id))
        rows = list((await self.session.execute(stmt.limit(limit + 1))).scalars().all())
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            KeysetCursor(sort_value=None, last_id=str(items[-1].id)).encode()
            if has_more and items
            else None
        )
        return CursorPage(items=items, next_cursor=next_cursor)

    async def training_run(self, run_id: uuid.UUID) -> TrainingRun | None:
        return await self.session.get(TrainingRun, run_id)

    async def snapshot(self, snapshot_id: uuid.UUID) -> DatasetSnapshot | None:
        return await self.session.get(DatasetSnapshot, snapshot_id)

    async def activate(self, model_id: uuid.UUID) -> MLModel | None:
        """Activa un modelo y desactiva el resto de su misma arquitectura.

        Solo un modelo activo por arquitectura: si hubiera dos, el pipeline elegiría
        de forma no determinista y se rompería la reproducibilidad (regla dura 3).
        """
        model = await self.get(model_id)
        if model is None:
            return None
        await self.session.execute(
            update(MLModel)
            .where(MLModel.architecture == model.architecture, MLModel.id != model_id)
            .values(is_active=False)
        )
        model.is_active = True
        await self.session.flush()
        return model
