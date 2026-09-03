"""Acceso a datos de ``sky_objects``, incluido el mapa de cobertura."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from geoalchemy2 import Geometry
from sqlalchemy import Row, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.models.sky_object import SkyObject
from app.repositories.base import CursorPage, KeysetCursor, parse_cursor

__all__ = ["CoverageRow", "SkyObjectRepository"]

#: Tamaño de la banda de latitud del mapa de cobertura, en grados. 15° reparte el
#: planeta en 12 bandas: suficiente para ver "falta hemisferio sur" sin pulverizar
#: el histograma en celdas de una foto.
LAT_BAND_DEG = 15

#: Bordes de las bandas de focal, en mm. Escala logarítmica porque lo que importa
#: es el factor entre focales, no la diferencia.
FOCAL_BANDS_MM: tuple[int, ...] = (0, 24, 50, 100, 200, 400, 800, 1600, 3200)


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """Una celda cruda del histograma tiempo × latitud × focal."""

    month: str
    lat_band_deg: int
    focal_band_mm: int
    photo_count: int
    mean_quality: float | None


class SkyObjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, object_id: uuid.UUID) -> SkyObject | None:
        return await self.session.get(SkyObject, object_id)

    async def resolve(self, token: str) -> SkyObject | None:
        """Resuelve ``?object=M31`` — UUID, designación o alias.

        Se intenta primero como UUID porque el contrato dice "alias **o id**".
        """
        token = token.strip()
        try:
            as_uuid = uuid.UUID(token)
        except ValueError:
            pass
        else:
            return await self.get(as_uuid)

        compact = token.replace(" ", "").upper()
        stmt = select(SkyObject).where(
            or_(
                func.upper(func.concat(SkyObject.catalog, SkyObject.catalog_number)) == compact,
                func.upper(SkyObject.common_name) == token.upper(),
                func.upper(SkyObject.common_name_es) == token.upper(),
                cast("Any", SkyObject.aliases).any(token),
            )
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_objects(
        self, *, limit: int, cursor: str | None, search: str | None = None
    ) -> CursorPage[SkyObject]:
        stmt = select(SkyObject).order_by(SkyObject.catalog, SkyObject.id)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    SkyObject.common_name.ilike(pattern),
                    SkyObject.common_name_es.ilike(pattern),
                    SkyObject.catalog_number.ilike(pattern),
                )
            )
        parsed = parse_cursor(cursor)
        if parsed is not None:
            stmt = stmt.where(SkyObject.id > uuid.UUID(parsed.last_id))
            stmt = stmt.order_by(None).order_by(SkyObject.id)
        rows = (await self.session.execute(stmt.limit(limit + 1))).scalars().all()
        has_more = len(rows) > limit
        items = list(rows[:limit])
        next_cursor = (
            KeysetCursor(sort_value=None, last_id=str(items[-1].id)).encode()
            if has_more and items
            else None
        )
        return CursorPage(items=items, next_cursor=next_cursor)

    async def coverage(self, object_id: uuid.UUID) -> list[CoverageRow]:
        """Histograma tiempo × latitud del observador × focal.

        Es lo que alimenta el widget "a este objeto le faltan tomas desde el
        hemisferio sur": sin diversidad de latitud no hay diversidad de ángulo
        paraláctico ni de ventana horaria, y sin diversidad de focal no hay
        diversidad de escala de muestreo.
        """
        lat = func.ST_Y(sa_cast(Photo.location, Geometry))
        lat_band = func.floor(lat / LAT_BAND_DEG) * LAT_BAND_DEG
        month = func.to_char(Photo.captured_at_utc, "YYYY-MM")
        focal_band = func.width_bucket(
            func.coalesce(Photo.focal_length_mm, 0.0),
            float(FOCAL_BANDS_MM[0]),
            float(FOCAL_BANDS_MM[-1]),
            len(FOCAL_BANDS_MM) - 1,
        )
        stmt = (
            select(
                month.label("month"),
                lat_band.label("lat_band"),
                focal_band.label("focal_bucket"),
                func.count().label("n"),
                func.avg(Photo.quality_score).label("mean_quality"),
            )
            .where(
                Photo.object_id == object_id,
                Photo.deleted_at.is_(None),
                Photo.status == PhotoStatus.READY,
            )
            .group_by(month, lat_band, focal_band)
            .order_by(month, lat_band, focal_band)
        )
        rows: list[Row[tuple[str | None, float | None, int | None, int, float | None]]] = list(
            (await self.session.execute(stmt)).all()  # type: ignore[arg-type]
        )
        out: list[CoverageRow] = []
        for month_value, lat_value, bucket, count, mean_quality in rows:
            index = max(0, min(len(FOCAL_BANDS_MM) - 1, int(bucket or 1) - 1))
            out.append(
                CoverageRow(
                    month=month_value or "unknown",
                    lat_band_deg=int(lat_value) if lat_value is not None else -999,
                    focal_band_mm=FOCAL_BANDS_MM[index],
                    photo_count=int(count),
                    mean_quality=float(mean_quality) if mean_quality is not None else None,
                )
            )
        return out
