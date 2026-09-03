"""Acceso a datos de ``photos``. Los servicios no escriben SQL a mano.

Aquí vive la consulta de ``GET /photos`` con PostGIS (cono en el cielo, cercanía en
la Tierra) y pgvector (``/photos/similar/{id}``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, time
from typing import Any, cast

from geoalchemy2 import Geography, Geometry
from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.models.sky_object import SkyObject
from app.repositories.base import CursorPage, KeysetCursor, as_cursor_value, parse_cursor
from app.schemas.search import PhotoSearchQuery, SortOrder

__all__ = ["PhotoRepository"]

#: Punto en el que el cono en el cielo deja de poder usarse tal cual: cerca de los
#: polos la comparación en RA se degenera y hay que caer al filtro esférico exacto.
_SPHERICAL_FALLBACK_DEC = 85.0


def _wkt_point(lat: float, lon: float) -> str:
    """WKT de un punto. PostGIS espera ``POINT(lon lat)``, en ese orden."""
    return f"SRID=4326;POINT({lon} {lat})"


class PhotoRepository:
    """Repositorio de fotos. Una instancia por petición, con su sesión."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Lectura simple
    # ------------------------------------------------------------------ #
    @staticmethod
    def _base_select() -> Select[tuple[Photo, float | None, float | None]]:
        """Selecciona la foto y su lat/lon ya extraídas de la geografía.

        El serializador necesita grados, no un ``WKBElement``; extraerlos aquí evita
        una dependencia de shapely en el proceso web.
        """
        geom = sa_cast(Photo.location, Geometry)
        return select(
            Photo,
            func.ST_Y(geom).label("lat_deg"),
            func.ST_X(geom).label("lon_deg"),
        )

    @staticmethod
    def _attach_coords(row: Any) -> Photo:
        """Cuelga ``lat_deg``/``lon_deg`` del objeto ORM para el serializador."""
        photo: Photo = row[0]
        photo.lat_deg = row[1]  # type: ignore[attr-defined]  # atributo transitorio
        photo.lon_deg = row[2]  # type: ignore[attr-defined]
        return photo

    async def get(self, photo_id: uuid.UUID, *, include_deleted: bool = False) -> Photo | None:
        stmt = self._base_select().where(Photo.id == photo_id)
        if not include_deleted:
            stmt = stmt.where(Photo.deleted_at.is_(None))
        row = (await self.session.execute(stmt)).first()
        return self._attach_coords(row) if row else None

    async def get_many(self, photo_ids: Sequence[uuid.UUID]) -> list[Photo]:
        """Devuelve las fotos existentes; el orden es el de ``photo_ids``."""
        if not photo_ids:
            return []
        stmt = self._base_select().where(Photo.id.in_(list(photo_ids)), Photo.deleted_at.is_(None))
        rows = (await self.session.execute(stmt)).all()
        by_id = {row[0].id: self._attach_coords(row) for row in rows}
        return [by_id[pid] for pid in photo_ids if pid in by_id]

    async def find_by_checksum(self, owner_id: uuid.UUID, checksum: bytes) -> Photo | None:
        """Deduplicación: UNIQUE ``(owner_id, checksum_sha256)``."""
        stmt = select(Photo).where(
            Photo.owner_id == owner_id,
            Photo.checksum_sha256 == checksum,
            Photo.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, photo: Photo) -> Photo:
        self.session.add(photo)
        await self.session.flush()
        return photo

    async def count_for_owner(self, owner_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(Photo.owner_id == owner_id, Photo.deleted_at.is_(None))
        return int((await self.session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------ #
    # Búsqueda
    # ------------------------------------------------------------------ #
    def _apply_filters(
        self, stmt: Select[Any], query: PhotoSearchQuery, resolved_object_id: uuid.UUID | None
    ) -> Select[Any]:
        stmt = stmt.where(Photo.deleted_at.is_(None), Photo.status == PhotoStatus.READY)

        if resolved_object_id is not None:
            stmt = stmt.where(Photo.object_id == resolved_object_id)
        elif query.object:
            # Alias textual: se busca en el array `aliases` y en los nombres comunes.
            alias = query.object.strip()
            stmt = stmt.where(
                Photo.object_id.in_(
                    select(SkyObject.id).where(
                        or_(
                            # `.any()` aquí es el comparador de ARRAY
                            # (SQL `= ANY(...)`), no el de la relación ORM.
                            cast("Any", SkyObject.aliases).any(alias),
                            func.lower(SkyObject.common_name) == alias.lower(),
                            func.lower(SkyObject.common_name_es) == alias.lower(),
                            func.concat(SkyObject.catalog, SkyObject.catalog_number)
                            == alias.replace(" ", ""),
                        )
                    )
                )
            )

        if query.owner_id is not None:
            stmt = stmt.where(Photo.owner_id == query.owner_id)

        # Cono en el cielo. Se usa la distancia esférica exacta sobre una esfera
        # unidad para no depender de una extensión de esferas: ST_DistanceSphere
        # sobre (ra, dec) tratados como lon/lat es exactamente la separación angular.
        if query.ra is not None and query.dec is not None and query.radius is not None:
            target = func.ST_SetSRID(func.ST_MakePoint(query.ra - 180.0, query.dec), 4326)
            field = func.ST_SetSRID(func.ST_MakePoint(Photo.ra_deg - 180.0, Photo.dec_deg), 4326)
            stmt = stmt.where(
                Photo.ra_deg.is_not(None),
                Photo.dec_deg.is_not(None),
                func.ST_DistanceSphere(field, target)
                <= query.radius * 111_195.0,  # 1° de arco sobre una esfera de radio R⊕
            )

        if query.near is not None:
            here = sa_cast(_wkt_point(query.near.lat, query.near.lon), Geography)
            stmt = stmt.where(
                Photo.location.is_not(None),
                func.ST_DWithin(Photo.location, here, query.near.km * 1000.0),
            )

        if query.date_from is not None:
            stmt = stmt.where(
                Photo.captured_at_utc >= datetime.combine(query.date_from, time.min, tzinfo=UTC)
            )
        if query.date_to is not None:
            stmt = stmt.where(
                Photo.captured_at_utc <= datetime.combine(query.date_to, time.max, tzinfo=UTC)
            )

        if query.min_focal is not None:
            stmt = stmt.where(Photo.focal_length_mm >= query.min_focal)
        if query.max_focal is not None:
            stmt = stmt.where(Photo.focal_length_mm <= query.max_focal)
        if query.filter:
            stmt = stmt.where(Photo.filter_name == query.filter)
        if query.min_quality is not None:
            stmt = stmt.where(Photo.quality_score >= query.min_quality)
        if query.tracked is not None:
            stmt = stmt.where(Photo.is_tracked.is_(query.tracked))
        if query.plate_solved:
            stmt = stmt.where(Photo.is_plate_solved.is_(True))

        licenses = query.allowed_licenses()
        if licenses:
            stmt = stmt.where(Photo.license.in_(licenses))
        if query.usable_for is not None and query.usable_for.value == "stacking":
            # El shortcut "stacking" no es solo licencia: también el consentimiento.
            stmt = stmt.where(Photo.allow_derivatives_in_stacks.is_(True))

        return stmt

    async def search(
        self, query: PhotoSearchQuery, *, resolved_object_id: uuid.UUID | None = None
    ) -> CursorPage[Photo]:
        """``GET /photos``. Paginación keyset sobre la clave de ordenación + id."""
        stmt = self._apply_filters(self._base_select(), query, resolved_object_id)
        cursor = parse_cursor(query.cursor)

        # El tipo de la columna de ordenación cambia según el criterio.
        sort_col: Any
        if query.sort is SortOrder.RECENT:
            sort_col = Photo.captured_at_utc
            stmt = stmt.order_by(sort_col.desc().nullslast(), Photo.id.desc())
            if cursor is not None:
                stmt = stmt.where(
                    or_(
                        sort_col < cursor.sort_value,
                        and_(
                            sort_col == cursor.sort_value,
                            Photo.id < uuid.UUID(cursor.last_id),
                        ),
                    )
                )
        elif query.sort is SortOrder.NEAREST and query.near is not None:
            here = sa_cast(_wkt_point(query.near.lat, query.near.lon), Geography)
            distance = func.ST_Distance(Photo.location, here)
            stmt = stmt.order_by(distance.asc(), Photo.id.asc())
            if cursor is not None:
                stmt = stmt.where(
                    or_(
                        distance > cursor.sort_value,
                        and_(
                            distance == cursor.sort_value,
                            Photo.id > uuid.UUID(cursor.last_id),
                        ),
                    )
                )
            stmt = stmt.add_columns(distance.label("sort_key"))
        else:
            sort_col = Photo.quality_score
            stmt = stmt.order_by(sort_col.desc().nullslast(), Photo.id.desc())
            if cursor is not None:
                stmt = stmt.where(
                    or_(
                        sort_col < cursor.sort_value,
                        and_(
                            sort_col == cursor.sort_value,
                            Photo.id < uuid.UUID(cursor.last_id),
                        ),
                    )
                )

        rows = (await self.session.execute(stmt.limit(query.limit + 1))).all()
        has_more = len(rows) > query.limit
        rows = rows[: query.limit]
        items = [self._attach_coords(row) for row in rows]

        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            if query.sort is SortOrder.RECENT:
                value = as_cursor_value(last.captured_at_utc)
            elif query.sort is SortOrder.NEAREST:
                value = as_cursor_value(rows[-1][-1])
            else:
                value = as_cursor_value(last.quality_score)
            next_cursor = KeysetCursor(sort_value=value, last_id=str(last.id)).encode()

        return CursorPage(items=items, next_cursor=next_cursor)

    async def similar(self, photo: Photo, limit: int = 20) -> list[Photo]:
        """``GET /photos/similar/{id}`` — vecinos por embedding (HNSW, coseno)."""
        if photo.embedding is None:
            return []
        distance = Photo.embedding.cosine_distance(photo.embedding)  # type: ignore[attr-defined]
        stmt = (
            self._base_select()
            .where(
                Photo.id != photo.id,
                Photo.deleted_at.is_(None),
                Photo.status == PhotoStatus.READY,
                Photo.embedding.is_not(None),
            )
            .order_by(distance.asc(), Photo.id.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [self._attach_coords(row) for row in rows]

    async def candidates_for_object(
        self, object_id: uuid.UUID, *, limit: int, min_quality: float = 0.0
    ) -> list[Photo]:
        """Fotos aptas para reconstruir: listas, resueltas y con consentimiento.

        El filtro de licencia **no** se hace aquí: lo decide ``domain.licensing`` y
        el servicio debe poder devolver la lista de bloqueadas al usuario en vez de
        esconderlas (regla 1 de ``docs/licensing.md``).
        """
        stmt = (
            self._base_select()
            .where(
                Photo.object_id == object_id,
                Photo.deleted_at.is_(None),
                Photo.status == PhotoStatus.READY,
                Photo.is_plate_solved.is_(True),
                or_(Photo.quality_score.is_(None), Photo.quality_score >= min_quality),
            )
            .order_by(Photo.quality_score.desc().nullslast(), Photo.id.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [self._attach_coords(row) for row in rows]

    async def increment_download(self, photo_id: uuid.UUID) -> None:
        """Suma una descarga.

        Mismo motivo que :meth:`increment_view` para ``synchronize_session=False``,
        y aquí importa el doble: justo después de contar la descarga el servicio
        fija ``license_locked_at`` sobre el mismo objeto ``Photo``. Con el objeto
        expirado esa escritura falla con ``MissingGreenlet``, así que ni se servía
        la descarga (500) ni se congelaba la licencia.
        """
        await self.session.execute(
            update(Photo)
            .where(Photo.id == photo_id)
            .values(download_count=Photo.download_count + 1)
            .execution_options(synchronize_session=False)
        )

    async def increment_view(self, photo_id: uuid.UUID) -> None:
        """Suma una visita. Contador de estadística, no de negocio.

        ``synchronize_session=False`` es obligatorio, no una optimización: con
        la estrategia por defecto SQLAlchemy **expira** el objeto ``Photo`` que
        el router acaba de cargar, y el primer acceso a un atributo durante la
        serialización dispara un refresco perezoso fuera del greenlet de
        asyncio (``MissingGreenlet``). Es decir, ``GET /photos/{id}`` devolvía
        500 para toda foto visible. El precio es que el ``view_count`` que sale
        en esta misma respuesta es el de antes de contarla, que es justamente
        lo que tiene sentido enseñar.
        """
        await self.session.execute(
            update(Photo)
            .where(Photo.id == photo_id)
            .values(view_count=Photo.view_count + 1)
            .execution_options(synchronize_session=False)
        )
