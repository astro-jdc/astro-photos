"""Fotos: subida en 3 pasos, lectura, edición, borrado y descarga."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import (
    CurrentDbUser,
    OptionalDbUser,
    get_photo_repository,
    get_photo_service,
    get_upload_service,
)
from app.repositories.photo import PhotoRepository
from app.schemas.photo import (
    DownloadOut,
    PhotoCompleteIn,
    PhotoOut,
    PhotoSummaryOut,
    PhotoUpdateIn,
    UploadRequestIn,
    UploadTicketOut,
)
from app.services.photo import PhotoService
from app.services.upload import UploadService

router = APIRouter(prefix="/photos", tags=["photos"])

PhotoServiceDep = Annotated[PhotoService, Depends(get_photo_service)]
UploadServiceDep = Annotated[UploadService, Depends(get_upload_service)]
PhotoRepoDep = Annotated[PhotoRepository, Depends(get_photo_repository)]


@router.post(
    "/uploads",
    response_model=UploadTicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="Paso 1: pide una URL presignada de subida",
)
async def create_upload(
    payload: UploadRequestIn, user: CurrentDbUser, service: UploadServiceDep
) -> UploadTicketOut:
    """Valida cuota, tipo y duplicado y devuelve un POST presignado de S3.

    Para ficheros > 100 MB devuelve en su lugar el bloque ``multipart``.
    El binario **nunca** pasa por este backend.
    """
    return await service.create_upload(user=user, request=payload)


@router.post(
    "/{photo_id}/complete",
    response_model=PhotoOut,
    summary="Paso 3: confirma la subida y encola la ingesta",
)
async def complete_upload(
    photo_id: UUID,
    payload: PhotoCompleteIn,
    user: CurrentDbUser,
    uploads: UploadServiceDep,
    photos: PhotoServiceDep,
) -> PhotoOut:
    photo = await uploads.complete_upload(user=user, photo_id=photo_id, payload=payload)
    return await photos.to_out(photo, viewer=user)


@router.get(
    "/similar/{photo_id}",
    response_model=list[PhotoSummaryOut],
    summary="Vecinos por embedding (pgvector HNSW)",
)
async def similar_photos(
    photo_id: UUID,
    viewer: OptionalDbUser,
    photos: PhotoServiceDep,
    repo: PhotoRepoDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[PhotoSummaryOut]:
    photo = await photos.get_visible(photo_id, viewer=viewer)
    neighbours = await repo.similar(photo, limit=limit)
    return [await photos.to_summary(p, viewer=viewer) for p in neighbours]


@router.get(
    "/{photo_id}",
    response_model=PhotoOut,
    summary="Metadata completa (la ubicación se ofusca según location_precision)",
)
async def read_photo(
    photo_id: UUID,
    viewer: OptionalDbUser,
    photos: PhotoServiceDep,
    repo: PhotoRepoDep,
) -> PhotoOut:
    photo = await photos.get_visible(photo_id, viewer=viewer)
    await repo.increment_view(photo.id)
    return await photos.to_out(photo, viewer=viewer)


@router.patch("/{photo_id}", response_model=PhotoOut, summary="Edita metadata")
async def update_photo(
    photo_id: UUID,
    payload: PhotoUpdateIn,
    user: CurrentDbUser,
    photos: PhotoServiceDep,
) -> PhotoOut:
    """La licencia solo puede **relajarse** si ``license_locked_at`` no es NULL."""
    photo = await photos.update(photo_id=photo_id, user=user, payload=payload)
    return await photos.to_out(photo, viewer=user)


@router.delete(
    "/{photo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete; 409 si participa en reconstrucciones publicadas",
)
async def delete_photo(photo_id: UUID, user: CurrentDbUser, photos: PhotoServiceDep) -> Response:
    await photos.soft_delete(photo_id=photo_id, user=user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{photo_id}/download",
    summary="302 a una URL firmada; incrementa download_count y audita",
    response_model=DownloadOut,
    responses={302: {"description": "Redirección a la URL firmada"}},
)
async def download_photo(
    photo_id: UUID,
    viewer: OptionalDbUser,
    photos: PhotoServiceDep,
    redirect: Annotated[
        bool, Query(description="false devuelve la URL en JSON en vez del 302")
    ] = True,
) -> Response:
    url, expires, photo = await photos.download(photo_id=photo_id, viewer=viewer)
    attribution = PhotoService.attribution_line(photo, str(photo.attribution_name or ""))
    if redirect:
        response = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
        # La atribución viaja también en cabecera: quien automatiza la descarga no
        # tiene por qué hacer otra llamada para saber a quién citar.
        response.headers["X-Attribution"] = attribution
        response.headers["X-License"] = photo.license.value
        return response
    return Response(
        content=DownloadOut(
            url=url,
            expires_at=expires,
            license=photo.license,
            attribution=attribution,
        ).model_dump_json(),
        media_type="application/json",
    )
