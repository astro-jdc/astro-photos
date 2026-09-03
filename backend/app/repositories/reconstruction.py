"""Acceso a datos de ``reconstructions`` y ``reconstruction_inputs``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import JobStatus
from app.models.reconstruction import Reconstruction, ReconstructionInput
from app.repositories.base import CursorPage, KeysetCursor, as_cursor_value, parse_cursor

__all__ = ["ReconstructionRepository"]

_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)


class ReconstructionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, reconstruction_id: uuid.UUID) -> Reconstruction | None:
        return await self.session.get(Reconstruction, reconstruction_id)

    async def get_by_idempotency_key(self, user_id: uuid.UUID, key: str) -> Reconstruction | None:
        """Regla dura 3: un POST repetido con la misma clave devuelve el mismo job."""
        stmt = select(Reconstruction).where(
            Reconstruction.requested_by == user_id,
            Reconstruction.idempotency_key == key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, reconstruction: Reconstruction) -> Reconstruction:
        """Inserta y **hace flush**: así el UNIQUE de idempotencia salta aquí, antes
        de que el servicio anuncie nada, y no al confirmar la transacción."""
        self.session.add(reconstruction)
        await self.session.flush()
        return reconstruction

    async def rollback(self) -> None:
        """Deshace la transacción en curso. Solo para recuperarse de un UNIQUE."""
        await self.session.rollback()

    async def add_inputs(self, rows: list[ReconstructionInput]) -> None:
        """Procedencia: se escribe **antes** de encolar, nunca después."""
        self.session.add_all(rows)
        await self.session.flush()

    async def inputs_for(self, reconstruction_id: uuid.UUID) -> list[ReconstructionInput]:
        stmt = (
            select(ReconstructionInput)
            .where(ReconstructionInput.reconstruction_id == reconstruction_id)
            .order_by(
                ReconstructionInput.was_rejected,
                ReconstructionInput.weight.desc(),
                ReconstructionInput.photo_id,
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_active_for_user(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            Reconstruction.requested_by == user_id,
            Reconstruction.status.in_(_ACTIVE),
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_last_24h(self, user_id: uuid.UUID) -> int:
        since = datetime.now(UTC) - timedelta(days=1)
        stmt = select(func.count()).where(
            Reconstruction.requested_by == user_id,
            Reconstruction.created_at >= since,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def photo_is_in_published(self, photo_id: uuid.UUID) -> bool:
        """¿La foto participa en alguna reconstrucción publicada?

        Es la condición de `409` de ``DELETE /photos/{id}`` y también la que congela
        la licencia.
        """
        stmt = (
            select(func.count())
            .select_from(ReconstructionInput)
            .join(
                Reconstruction,
                Reconstruction.id == ReconstructionInput.reconstruction_id,
            )
            .where(
                ReconstructionInput.photo_id == photo_id,
                ReconstructionInput.was_rejected.is_(False),
                Reconstruction.status == JobStatus.SUCCEEDED,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one()) > 0

    async def list_public(
        self,
        *,
        limit: int,
        cursor: str | None,
        object_id: uuid.UUID | None = None,
        requested_by: uuid.UUID | None = None,
    ) -> CursorPage[Reconstruction]:
        stmt = select(Reconstruction).order_by(
            Reconstruction.created_at.desc(), Reconstruction.id.desc()
        )
        if requested_by is not None:
            stmt = stmt.where(Reconstruction.requested_by == requested_by)
        else:
            stmt = stmt.where(
                Reconstruction.is_public.is_(True),
                Reconstruction.status == JobStatus.SUCCEEDED,
            )
        if object_id is not None:
            stmt = stmt.where(Reconstruction.object_id == object_id)

        parsed = parse_cursor(cursor)
        if parsed is not None:
            stmt = stmt.where(
                Reconstruction.created_at < datetime.fromisoformat(str(parsed.sort_value))
            )
        rows = list((await self.session.execute(stmt.limit(limit + 1))).scalars().all())
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            KeysetCursor(
                sort_value=as_cursor_value(items[-1].created_at), last_id=str(items[-1].id)
            ).encode()
            if has_more and items
            else None
        )
        return CursorPage(items=items, next_cursor=next_cursor)
