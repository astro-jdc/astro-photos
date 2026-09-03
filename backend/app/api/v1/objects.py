"""``/objects`` 🔓 — catálogo y mapa de cobertura."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import SettingsDep, get_object_repository, get_object_service
from app.repositories.sky_object import SkyObjectRepository
from app.schemas.common import Page
from app.schemas.sky_object import CoverageOut, ObjectOut
from app.services.sky_object import ObjectService

router = APIRouter(prefix="/objects", tags=["objects"])

ObjectServiceDep = Annotated[ObjectService, Depends(get_object_service)]
ObjectRepoDep = Annotated[SkyObjectRepository, Depends(get_object_repository)]


@router.get("", response_model=Page[ObjectOut], summary="Catálogo de objetos")
async def list_objects(
    settings: SettingsDep,
    repo: ObjectRepoDep,
    q: Annotated[str | None, Query(description="Búsqueda por nombre o número")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[ObjectOut]:
    page = await repo.list_objects(
        limit=min(limit, settings.max_page_size), cursor=cursor, search=q
    )
    return Page[ObjectOut](
        items=[ObjectService.to_out(o) for o in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{object_id}", response_model=ObjectOut, summary="Un objeto del catálogo")
async def read_object(object_id: UUID, service: ObjectServiceDep) -> ObjectOut:
    return ObjectService.to_out(await service.get(object_id))


@router.get(
    "/{object_id}/coverage",
    response_model=CoverageOut,
    summary="Mapa de cobertura: fotos por tiempo × latitud × focal",
)
async def object_coverage(object_id: UUID, service: ObjectServiceDep) -> CoverageOut:
    """Alimenta el widget «a este objeto le faltan tomas desde el hemisferio sur»."""
    return await service.coverage(object_id)
