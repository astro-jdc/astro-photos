"""``/reconstructions`` — preview, creación, estado, SSE, procedencia y resultado."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from app.api.deps import (
    CurrentDbUser,
    IdempotencyKey,
    OptionalDbUser,
    SettingsDep,
    get_reconstruction_repository,
    get_reconstruction_service,
    get_storage,
)
from app.core.errors import NotFoundError
from app.models.enums import JobStatus
from app.models.reconstruction import Reconstruction
from app.repositories.reconstruction import ReconstructionRepository
from app.schemas.common import Page
from app.schemas.reconstruction import (
    ReconstructionCreateIn,
    ReconstructionInputOut,
    ReconstructionOut,
    ReconstructionPlanOut,
    ReconstructionResultOut,
)
from app.services.reconstruction import ReconstructionService
from app.services.storage import StorageService

router = APIRouter(prefix="/reconstructions", tags=["reconstructions"])

ServiceDep = Annotated[ReconstructionService, Depends(get_reconstruction_service)]
RepoDep = Annotated[ReconstructionRepository, Depends(get_reconstruction_repository)]
StorageDep = Annotated[StorageService, Depends(get_storage)]

#: Cadencia del SSE. 2 s es suficiente para una barra de progreso y no castiga la
#: base de datos con un job de horas.
SSE_POLL_SECONDS = 2.0
#: Corta el stream aunque el job siga: el cliente reconecta (EventSource lo hace solo).
SSE_MAX_SECONDS = 900.0


def _to_out(job: Reconstruction, preview_url: str | None = None) -> ReconstructionOut:
    return ReconstructionOut(
        id=job.id,
        requested_by=job.requested_by,
        object_id=job.object_id,
        pipeline=job.pipeline,
        pipeline_version=job.pipeline_version,
        model_id=job.model_id,
        params=job.params,
        status=job.status,
        progress=job.progress,
        input_count=job.input_count,
        license=job.license,
        metrics=job.metrics,
        error_message=job.error_message,
        preview_url=preview_url,
        started_at=job.started_at,
        finished_at=job.finished_at,
        compute_seconds=job.compute_seconds,
        cost_usd_estimate=job.cost_usd_estimate,
        is_public=job.is_public,
        created_at=job.created_at,
    )


async def _visible(
    repo: ReconstructionRepository, reconstruction_id: UUID, viewer_id: UUID | None
) -> Reconstruction:
    job = await repo.get(reconstruction_id)
    if job is None:
        raise NotFoundError("La reconstrucción no existe.")
    if not job.is_public and job.requested_by != viewer_id:
        raise NotFoundError("La reconstrucción no existe.")
    return job


@router.post(
    "/preview",
    response_model=ReconstructionPlanOut,
    summary="Plan del job sin encolar nada",
)
async def preview_reconstruction(
    payload: ReconstructionCreateIn, user: CurrentDbUser, service: ServiceDep
) -> ReconstructionPlanOut:
    """Devuelve fotos seleccionadas, rechazadas y por qué, licencia resultante,
    fotos bloqueadas y coste estimado. El frontend siempre llama aquí primero."""
    return await service.preview(user=user, payload=payload)


@router.post(
    "",
    response_model=ReconstructionOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Crea el job de reconstrucción",
)
async def create_reconstruction(
    payload: ReconstructionCreateIn,
    user: CurrentDbUser,
    service: ServiceDep,
    settings: SettingsDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ReconstructionOut:
    """Responde **202** con ``Location``. Idempotente por ``Idempotency-Key``."""
    job, created = await service.create(user=user, payload=payload, idempotency_key=idempotency_key)
    response.headers["Location"] = f"{settings.api_prefix}/reconstructions/{job.id}"
    if not created:
        response.headers["Idempotent-Replay"] = "true"
    return _to_out(job)


@router.get("", response_model=Page[ReconstructionOut], summary="Galería pública")
async def list_reconstructions(
    repo: RepoDep,
    settings: SettingsDep,
    object_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[ReconstructionOut]:
    page = await repo.list_public(
        limit=min(limit, settings.max_page_size), cursor=cursor, object_id=object_id
    )
    return Page[ReconstructionOut](
        items=[_to_out(j) for j in page.items], next_cursor=page.next_cursor
    )


@router.get("/{reconstruction_id}", response_model=ReconstructionOut, summary="Estado")
async def read_reconstruction(
    reconstruction_id: UUID,
    viewer: OptionalDbUser,
    repo: RepoDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ReconstructionOut:
    job = await _visible(repo, reconstruction_id, viewer.id if viewer else None)
    preview_url: str | None = None
    if job.s3_key_preview:
        preview_url, _ = await storage.presigned_get(
            bucket=settings.s3_bucket_derived, key=job.s3_key_preview
        )
    return _to_out(job, preview_url)


@router.get(
    "/{reconstruction_id}/events",
    summary="SSE con el progreso en vivo",
    response_class=EventSourceResponse,
)
async def reconstruction_events(
    reconstruction_id: UUID,
    request: Request,
    viewer: OptionalDbUser,
    repo: RepoDep,
) -> EventSourceResponse:
    """Emite un evento cada ~2 s hasta que el job termina o el cliente se va."""
    await _visible(repo, reconstruction_id, viewer.id if viewer else None)

    async def stream() -> AsyncIterator[dict[str, str]]:
        elapsed = 0.0
        last_signature: tuple[str, float] | None = None
        while elapsed < SSE_MAX_SECONDS:
            if await request.is_disconnected():
                return
            job = await repo.get(reconstruction_id)
            if job is None:
                yield {"event": "error", "data": json.dumps({"detail": "desaparecida"})}
                return
            signature = (job.status.value, round(job.progress, 4))
            if signature != last_signature:
                last_signature = signature
                yield {
                    "event": "progress",
                    "data": json.dumps(
                        {
                            "reconstruction_id": str(job.id),
                            "status": job.status.value,
                            "progress": job.progress,
                            "metrics": job.metrics,
                            "message": job.error_message,
                            "at": datetime.now(UTC).isoformat(),
                        }
                    ),
                }
            if job.status in (
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                yield {"event": "done", "data": json.dumps({"status": job.status.value})}
                return
            await asyncio.sleep(SSE_POLL_SECONDS)
            elapsed += SSE_POLL_SECONDS
        # Corte limpio: EventSource reconecta solo.
        yield {"event": "timeout", "data": json.dumps({"reconnect": True})}

    return EventSourceResponse(stream())


@router.get(
    "/{reconstruction_id}/inputs",
    response_model=list[ReconstructionInputOut],
    summary="Procedencia: qué entró, con qué peso y qué se descartó",
)
async def reconstruction_inputs(
    reconstruction_id: UUID, viewer: OptionalDbUser, repo: RepoDep
) -> list[ReconstructionInputOut]:
    await _visible(repo, reconstruction_id, viewer.id if viewer else None)
    rows = await repo.inputs_for(reconstruction_id)
    return [
        ReconstructionInputOut(
            photo_id=r.photo_id,
            weight=r.weight,
            was_rejected=r.was_rejected,
            rejection_reason=r.rejection_reason,
            alignment_rms_px=r.alignment_rms_px,
            snapshot_license=r.snapshot_license,
            snapshot_attribution_name=r.snapshot_attribution_name,
        )
        for r in rows
    ]


@router.get(
    "/{reconstruction_id}/result",
    response_model=ReconstructionResultOut,
    summary="URLs firmadas del TIFF/FITS y ATTRIBUTION.md",
)
async def reconstruction_result(
    reconstruction_id: UUID,
    viewer: OptionalDbUser,
    repo: RepoDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ReconstructionResultOut:
    job = await _visible(repo, reconstruction_id, viewer.id if viewer else None)
    bucket = settings.s3_bucket_derived
    expires: datetime | None = None

    async def sign(key: str | None) -> str | None:
        nonlocal expires
        if not key:
            return None
        url, exp = await storage.presigned_get(bucket=bucket, key=key)
        expires = exp
        return url

    return ReconstructionResultOut(
        reconstruction_id=job.id,
        status=job.status,
        license=job.license,
        result_url=await sign(job.s3_key_result),
        preview_url=await sign(job.s3_key_preview),
        report_url=await sign(job.s3_key_report),
        attribution_url=await sign(job.s3_key_attribution),
        provenance_url=await sign(job.s3_key_provenance),
        expires_at=expires,
    )


@router.delete(
    "/{reconstruction_id}",
    response_model=ReconstructionOut,
    summary="Cancela si está en cola o corriendo",
)
async def cancel_reconstruction(
    reconstruction_id: UUID, user: CurrentDbUser, service: ServiceDep
) -> ReconstructionOut:
    job = await service.cancel(reconstruction_id=reconstruction_id, user=user)
    return _to_out(job)


__all__ = ["router"]
