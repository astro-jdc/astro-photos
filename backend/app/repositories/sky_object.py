"""Acceso a datos de ``sky_objects``, incluido el mapa de cobertura."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from geoalchemy2 import Geometry
from sqlalchemy import Row, case, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.location import LocationPrecision
from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.models.sky_object import SkyObject
from app.repositories.base import CursorPage, KeysetCursor, parse_cursor

__all__ = ["CoverageRow", "RawSiteRow", "SkyObjectRepository"]

#: Tamaño de la banda de latitud del mapa de cobertura, en grados. 15° reparte el
#: planeta en 12 bandas: suficiente para ver "falta hemisferio sur" sin pulverizar
#: el histograma en celdas de una foto.
LAT_BAND_DEG = 15

#: Bordes de las bandas de focal, en mm. Escala logarítmica porque lo que importa
#: es el factor entre focales, no la diferencia.
FOCAL_BANDS_MM: tuple[int, ...] = (0, 24, 50, 100, 200, 400, 800, 1600, 3200)

#: Banda de latitud de las fotos sin posición conocida.
UNKNOWN_LAT_BIN = -999

#: Tamaño del bin temporal. El eje del widget es mensual.
PERIOD_BIN = "month"


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """Una celda cruda del histograma tiempo × latitud × focal."""

    period: str
    lat_bin: int
    focal_bin: int
    count: int
    #: La **mejor** calidad de la celda, no la media: lo que dice si a este objeto le
    #: falta cobertura ahí es si existe *alguna* toma buena, no el promedio.
    best_quality: float | None


@dataclass(frozen=True, slots=True)
class RawSiteRow:
    """Una posición de observación cruda, con la precisión que autorizó su autor.

    **No se agrega antes de ofuscar.** Agregar y ofuscar después filtraría la
    posición exacta por la puerta de atrás: bastaría con que una sola foto exacta
    cayera en el grupo para que el centroide publicado delatase el sitio. Por eso el
    repositorio devuelve un grupo por (posición, precisión, país) y la agregación
    final la hace el servicio **después** de aplicar ``obfuscate_location`` a cada
    grupo con su propia precisión.
    """

    lat: float
    lon: float
    precision: LocationPrecision
    country_code: str | None
    accuracy_m: float | None
    elevation_m: float | None
    count: int


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
        # Una banda de 15° sigue siendo una posición. Quien pidió `hidden` no
        # autorizó publicar su latitud, ni siquiera redondeada a 1700 km, así que
        # cae en la banda "desconocida": sigue contando en los ejes de tiempo y
        # focal, pero no aporta latitud al mapa.
        lat_band = case(
            (
                Photo.location_precision == LocationPrecision.HIDDEN,
                None,
            ),
            else_=func.floor(lat / LAT_BAND_DEG) * LAT_BAND_DEG,
        )
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
                func.max(Photo.quality_score).label("best_quality"),
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
        for period, lat_value, bucket, count, best_quality in rows:
            index = max(0, min(len(FOCAL_BANDS_MM) - 1, int(bucket or 1) - 1))
            out.append(
                CoverageRow(
                    period=period or "unknown",
                    lat_bin=int(lat_value) if lat_value is not None else UNKNOWN_LAT_BIN,
                    focal_bin=FOCAL_BANDS_MM[index],
                    count=int(count),
                    best_quality=float(best_quality) if best_quality is not None else None,
                )
            )
        return out

    async def raw_sites(self, object_id: uuid.UUID) -> list[RawSiteRow]:
        """Posiciones de observación **sin ofuscar**, agrupadas por posición exacta.

        Devolver esto crudo es deliberado y seguro: nunca sale del backend. El
        servicio aplica :func:`obfuscate_location` a cada grupo con la precisión de
        sus propias fotos y solo entonces vuelve a agregar. Las fotos con precisión
        ``hidden`` se excluyen ya aquí: no hay ningún nivel de agregación en el que
        deban contribuir a un mapa.
        """
        geom = sa_cast(Photo.location, Geometry)
        stmt = (
            select(
                func.ST_Y(geom).label("lat"),
                func.ST_X(geom).label("lon"),
                Photo.location_precision,
                Photo.country_code,
                func.max(Photo.location_accuracy_m).label("accuracy_m"),
                func.max(Photo.elevation_m).label("elevation_m"),
                func.count().label("n"),
            )
            .where(
                Photo.object_id == object_id,
                Photo.deleted_at.is_(None),
                Photo.status == PhotoStatus.READY,
                Photo.location.is_not(None),
                Photo.location_precision != LocationPrecision.HIDDEN,
            )
            .group_by(
                func.ST_Y(geom),
                func.ST_X(geom),
                Photo.location_precision,
                Photo.country_code,
            )
        )
        return [
            RawSiteRow(
                lat=float(row[0]),
                lon=float(row[1]),
                precision=LocationPrecision(row[2]),
                country_code=row[3],
                accuracy_m=float(row[4]) if row[4] is not None else None,
                elevation_m=float(row[5]) if row[5] is not None else None,
                count=int(row[6]),
            )
            for row in (await self.session.execute(stmt)).all()
        ]
