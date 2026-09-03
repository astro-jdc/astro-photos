"""Contadores agregados para ``GET /stats``.

Los cinco números salen de **una sola consulta** por tabla, sin joins: son barridos
completos y a escala de catálogo conviene que sean pocos y baratos. La caché de 5
minutos del servicio hace el resto.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import JobStatus, PhotoStatus
from app.models.photo import Photo
from app.models.reconstruction import Reconstruction
from app.models.sky_object import SkyObject

__all__ = ["StatsRepository", "StatsSnapshot"]


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Los contadores crudos, sin formato ni caché."""

    photo_count: int
    object_count: int
    reconstruction_count: int
    contributor_count: int
    total_exposure_seconds: float


class StatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def snapshot(self) -> StatsSnapshot:
        """Cuenta lo que de verdad es público.

        * Fotos: ``status='ready'`` y ``deleted_at IS NULL``. Una foto en
          ``processing`` o en cuarentena no está en el repositorio todavía.
        * Reconstrucciones: ``succeeded`` **y** ``is_public``. Contar trabajo privado
          o a medias sería inflar la cifra de portada.
        * Contribuyentes: autores distintos con al menos una foto contada, no usuarios
          registrados: la portada dice cuánta gente ha aportado cielo, no cuánta se
          ha dado de alta.
        """
        visible = (Photo.status == PhotoStatus.READY, Photo.deleted_at.is_(None))

        photo_row = (
            await self.session.execute(
                select(
                    func.count(Photo.id),
                    func.count(func.distinct(Photo.owner_id)),
                    func.coalesce(func.sum(Photo.exposure_seconds), 0.0),
                ).where(*visible)
            )
        ).one()

        object_count = (await self.session.execute(select(func.count(SkyObject.id)))).scalar_one()

        reconstruction_count = (
            await self.session.execute(
                select(func.count(Reconstruction.id)).where(
                    Reconstruction.status == JobStatus.SUCCEEDED,
                    Reconstruction.is_public.is_(True),
                )
            )
        ).scalar_one()

        return StatsSnapshot(
            photo_count=int(photo_row[0]),
            object_count=int(object_count),
            reconstruction_count=int(reconstruction_count),
            contributor_count=int(photo_row[1]),
            total_exposure_seconds=float(photo_row[2]),
        )
