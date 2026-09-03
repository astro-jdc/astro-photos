"""``/reconstructions`` — preview, creación, estado, SSE, procedencia y resultado."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
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
    get_photo_repository,
    get_reconstruction_repository,
    get_reconstruction_service,
    get_storage,
)
from app.core.errors import NotFoundError, UnauthorizedError
from app.db.session import session_scope
from app.models.enums import JobStatus
from app.models.reconstruction import Reconstruction
from app.repositories.photo import PhotoRepository
from app.repositories.reconstruction import ReconstructionRepository
from app.schemas.common import Page
from app.schemas.reconstruction import (
    BestSingleFrameOut,
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
PhotoRepoDep = Annotated[PhotoRepository, Depends(get_photo_repository)]
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
    viewer: OptionalDbUser,
    object_id: UUID | None = None,
    mine: Annotated[
        bool, Query(description="Solo las mías, incluidas las privadas y en curso")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[ReconstructionOut]:
    """Sin `mine` devuelve la galería pública; con `mine` exige estar identificado."""
    if mine and viewer is None:
        raise UnauthorizedError("`mine=true` requiere autenticación.")
    page = await repo.list_public(
        limit=min(limit, settings.max_page_size),
        cursor=cursor,
        object_id=object_id,
        requested_by=viewer.id if (mine and viewer is not None) else None,
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
    # El docstring es la descripción pública de la operación en el OpenAPI, así que
    # el razonamiento de implementación va aquí:
    #
    # El generador abre su **propia** sesión en cada sondeo. El cuerpo de una
    # respuesta en streaming se produce *después* de que la unidad de trabajo de la
    # petición se haya confirmado y cerrado, así que dentro del generador no se puede
    # usar `repo`: esa sesión ya no existe. Además, sostener una conexión del pool
    # durante los 15 minutos que puede durar un SSE lo agotaría con una docena de
    # espectadores.
    #
    # La comprobación de visibilidad sí usa la sesión de la petición: ocurre dentro
    # del handler, antes de devolver la respuesta.
    await _visible(repo, reconstruction_id, viewer.id if viewer else None)

    async def poll() -> Reconstruction | None:
        """Un sondeo = una sesión corta, abierta y cerrada al momento."""
        async with session_scope() as session:
            job = await ReconstructionRepository(session).get(reconstruction_id)
            if job is None:
                return None
            # `expire_on_commit=False`, así que los atributos siguen siendo legibles
            # cuando la sesión se cierra al salir del `with`.
            return job

    async def stream() -> AsyncIterator[dict[str, str]]:
        elapsed = 0.0
        last_signature: tuple[str, float] | None = None
        while elapsed < SSE_MAX_SECONDS:
            if await request.is_disconnected():
                return
            job = await poll()
            if job is None:
                yield {"event": "error", "data": json.dumps({"detail": "desaparecida"})}
                return
            stage = (job.metrics or {}).get("stage") if job.metrics else None
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
                            "stage": stage,
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
    summary="URLs firmadas del TIFF/FITS, mapas, ATTRIBUTION.md y el mejor frame",
)
async def reconstruction_result(
    reconstruction_id: UUID,
    viewer: OptionalDbUser,
    repo: RepoDep,
    photos: PhotoRepoDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ReconstructionResultOut:
    """Incluye ``best_single_frame``: la **comparación honesta**.

    Sin el mejor frame individual de las entradas la interfaz afirmaría una mejora
    que no enseña, y el usuario no podría juzgar si la reconstrucción aportó algo
    sobre la mejor toma que ya existía.
    """
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

    best: BestSingleFrameOut | None = None
    if job.status is JobStatus.SUCCEEDED:
        best = await _best_single_frame(repo, photos, job.id, sign)

    return ReconstructionResultOut(
        reconstruction_id=job.id,
        status=job.status,
        license=job.license,
        pipeline=job.pipeline,
        pipeline_version=job.pipeline_version,
        model_id=job.model_id,
        result_url=await sign(job.s3_key_result),
        preview_url=await sign(job.s3_key_preview),
        uncertainty_map_url=await sign(job.s3_key_uncertainty),
        weight_map_url=await sign(job.s3_key_weight_map),
        provenance_json_url=await sign(job.s3_key_provenance),
        attribution_md_url=await sign(job.s3_key_attribution),
        report_url=await sign(job.s3_key_report),
        best_single_frame=best,
        metrics=job.metrics,
        expires_at=expires,
    )


async def _best_single_frame(
    repo: ReconstructionRepository,
    photos: PhotoRepository,
    reconstruction_id: UUID,
    sign: Callable[[str | None], Awaitable[str | None]],
) -> BestSingleFrameOut | None:
    """El mejor frame **realmente usado**, no el mejor candidato.

    Se ordena por ``quality_score`` y se desempata por ``photo_id`` para que la
    comparación que ve el usuario sea siempre la misma (regla dura 3: nada de
    depender del orden de la base de datos).
    """
    rows = [row for row in await repo.inputs_for(reconstruction_id) if not row.was_rejected]
    if not rows:
        return None
    used = await photos.get_many([row.photo_id for row in rows])
    if not used:
        return None
    best = min(
        used,
        key=lambda p: (
            -(p.quality_score if p.quality_score is not None else -1.0),
            str(p.id),
        ),
    )
    return BestSingleFrameOut(
        photo_id=best.id,
        preview_url=await sign(best.s3_key_preview),
        fwhm_arcsec=best.fwhm_arcsec,
        snr_estimate=best.snr_estimate,
        quality_score=best.quality_score,
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
