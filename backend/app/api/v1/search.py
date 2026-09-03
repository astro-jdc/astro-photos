"""``GET /photos`` 🔓 — búsqueda con filtros combinables.

Vive en su propio router porque la ruta colisiona con el prefijo ``/photos`` del
router de fotos y el orden de registro importa: este se monta antes.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    OptionalDbUser,
    SettingsDep,
    get_object_repository,
    get_photo_repository,
    get_photo_service,
)
from app.core.errors import BadRequestError, NotFoundError
from app.domain.licensing import LicenseCode
from app.repositories.photo import PhotoRepository
from app.repositories.sky_object import SkyObjectRepository
from app.schemas.common import Page
from app.schemas.photo import PhotoSummaryOut
from app.schemas.search import GeoNear, PhotoSearchQuery, SortOrder, UsableFor
from app.services.photo import PhotoService

router = APIRouter(tags=["search"])


@router.get(
    "/photos",
    response_model=Page[PhotoSummaryOut],
    summary="Búsqueda de fotos con filtros combinables",
)
async def search_photos(
    settings: SettingsDep,
    viewer: OptionalDbUser,
    repo: Annotated[PhotoRepository, Depends(get_photo_repository)],
    objects: Annotated[SkyObjectRepository, Depends(get_object_repository)],
    photos: Annotated[PhotoService, Depends(get_photo_service)],
    object: Annotated[str | None, Query(description="Alias o id del objeto")] = None,
    ra: Annotated[float | None, Query(ge=0, lt=360)] = None,
    dec: Annotated[float | None, Query(ge=-90, le=90)] = None,
    radius: Annotated[float | None, Query(gt=0, le=180, description="Grados")] = None,
    near: Annotated[
        str | None, Query(description="`lat,lon` de la posición del observador")
    ] = None,
    km: Annotated[float | None, Query(gt=0, le=20000)] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    min_focal: Annotated[float | None, Query(gt=0)] = None,
    max_focal: Annotated[float | None, Query(gt=0)] = None,
    filter: Annotated[str | None, Query(description="Nombre de filtro, p. ej. `Ha`")] = None,
    license: Annotated[
        str | None, Query(description="Lista separada por comas de códigos de licencia")
    ] = None,
    usable_for: UsableFor | None = None,
    min_quality: Annotated[float | None, Query(ge=0, le=1)] = None,
    tracked: bool | None = None,
    owner_id: UUID | None = None,
    plate_solved: bool | None = None,
    sort: SortOrder = SortOrder.QUALITY,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[PhotoSummaryOut]:
    """Ver ``docs/api.md`` para la semántica de cada filtro."""
    geo: GeoNear | None = None
    if near is not None:
        try:
            lat_str, lon_str = near.split(",")
            geo = GeoNear(lat=float(lat_str), lon=float(lon_str), km=km or 50.0)
        except (ValueError, TypeError) as exc:
            raise BadRequestError(
                "El parámetro `near` debe tener la forma `lat,lon`.",
                errors=[{"pointer": "/near", "detail": "formato inválido"}],
            ) from exc
    elif km is not None:
        raise BadRequestError(
            "`km` solo tiene sentido junto a `near`.",
            errors=[{"pointer": "/km", "detail": "falta `near`"}],
        )

    licenses: list[LicenseCode] | None = None
    if license:
        try:
            licenses = [LicenseCode(item.strip()) for item in license.split(",") if item.strip()]
        except ValueError as exc:
            raise BadRequestError(
                "Alguno de los códigos de licencia no existe.",
                errors=[{"pointer": "/license", "detail": str(exc)}],
            ) from exc

    # `from` es palabra reservada: el schema lo declara con alias, así que el
    # objeto se construye por validación en vez de por argumentos con nombre.
    query = PhotoSearchQuery.model_validate(
        {
            "object": object,
            "ra": ra,
            "dec": dec,
            "radius": radius,
            "near": geo,
            "from": date_from,
            "to": date_to,
            "min_focal": min_focal,
            "max_focal": max_focal,
            "filter": filter,
            "license": licenses,
            "usable_for": usable_for,
            "min_quality": min_quality,
            "tracked": tracked,
            "owner_id": owner_id,
            "plate_solved": plate_solved,
            "sort": sort,
            "limit": min(limit, settings.max_page_size),
            "cursor": cursor,
        }
    )

    resolved_object_id: UUID | None = None
    if object:
        obj = await objects.resolve(object)
        if obj is None:
            raise NotFoundError(f"No conozco el objeto «{object}».")
        resolved_object_id = obj.id

    page = await repo.search(query, resolved_object_id=resolved_object_id)
    return Page[PhotoSummaryOut](
        items=[await photos.to_summary(p, viewer=viewer) for p in page.items],
        next_cursor=page.next_cursor,
    )
