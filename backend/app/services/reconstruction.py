"""Reconstrucciones: preview, validación de licencias, procedencia y encolado.

El orden importa y no es negociable:

1. Se resuelven los candidatos (lista explícita o ``selector``).
2. Se pide a ``domain.licensing`` la licencia de salida. Si hay bloqueadas, **se
   aborta**: nunca se degrada el job quitando fotos por nuestra cuenta.
3. Se seleccionan los frames con ``domain.selection`` (calidad + diversidad).
4. Se escribe ``reconstruction_inputs`` con el peso y la licencia congelada.
5. **Solo entonces** se encola en SQS.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    LicenseBlockedError,
    NotFoundError,
    RateLimitError,
    UnprocessableError,
)
from app.domain.astro import diffraction_limit_arcsec
from app.domain.licensing import PhotoLicenseFacts, resolve_output_license
from app.domain.selection import FrameCandidate, SelectionResult, select_frames
from app.models.enums import JobStatus, PhotoStatus
from app.models.photo import Photo
from app.models.reconstruction import Reconstruction, ReconstructionInput
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.photo import PhotoRepository
from app.repositories.reconstruction import ReconstructionRepository
from app.repositories.sky_object import SkyObjectRepository
from app.schemas.license import BlockedPhotoOut
from app.schemas.reconstruction import (
    CostEstimateOut,
    ReconstructionCreateIn,
    ReconstructionPlanOut,
    RejectedFrameOut,
    SelectedFrameOut,
)

__all__ = ["ReconstructionService"]

log = structlog.get_logger(__name__)

#: Coste orientativo de un core-segundo de AWS Batch (c6i spot, eu-west-1).
USD_PER_COMPUTE_SECOND = 0.000012
#: Segundos de cómputo por frame y pipeline, medidos a ojo en desarrollo. Es una
#: estimación de producto, no una promesa: el número real lo devuelve Batch.
SECONDS_PER_FRAME: dict[str, float] = {
    "classical-stack-v1": 1.5,
    "drizzle-v1": 4.0,
    "burst-sr-v1": 12.0,
}


class ReconstructionService:
    def __init__(
        self,
        *,
        photos: PhotoRepository,
        reconstructions: ReconstructionRepository,
        objects: SkyObjectRepository,
        audit: AuditRepository,
        queue: Any,
        settings: Settings,
        pipeline_version: str = "dev",
    ) -> None:
        self.photos = photos
        self.reconstructions = reconstructions
        self.objects = objects
        self.audit = audit
        self.queue = queue
        self.settings = settings
        self.pipeline_version = pipeline_version

    # ------------------------------------------------------------------ #
    async def _gather_candidates(
        self, payload: ReconstructionCreateIn
    ) -> tuple[list[Photo], uuid.UUID | None]:
        """Resuelve la lista de fotos candidatas y el objeto objetivo."""
        if payload.photo_ids is not None:
            photos = await self.photos.get_many(payload.photo_ids)
            found = {p.id for p in photos}
            missing = [pid for pid in payload.photo_ids if pid not in found]
            if missing:
                raise UnprocessableError(
                    "Algunas fotos no existen o no son accesibles.",
                    errors=[
                        {"pointer": "/photo_ids", "detail": str(pid), "code": "not_found"}
                        for pid in missing
                    ],
                )
            not_ready = [p for p in photos if p.status is not PhotoStatus.READY]
            if not_ready:
                raise UnprocessableError(
                    "Algunas fotos todavía no están procesadas.",
                    errors=[
                        {
                            "pointer": "/photo_ids",
                            "detail": f"{p.id} está en estado {p.status.value}",
                            "code": "not_ready",
                        }
                        for p in not_ready
                    ],
                )
            object_id = payload.object_id
            if object_id is None:
                object_ids = {p.object_id for p in photos if p.object_id is not None}
                object_id = next(iter(object_ids)) if len(object_ids) == 1 else None
            return photos, object_id

        # Selector: mismo lenguaje que la búsqueda.
        assert payload.selector is not None  # garantizado por el validador del schema
        object_id = payload.object_id
        if object_id is None and payload.selector.object:
            obj = await self.objects.resolve(payload.selector.object)
            if obj is None:
                raise NotFoundError(f"No conozco el objeto «{payload.selector.object}».")
            object_id = obj.id
        if object_id is None:
            raise UnprocessableError("No se pudo determinar el objeto objetivo.")

        photos = await self.photos.candidates_for_object(
            object_id,
            limit=min(
                self.settings.max_reconstruction_inputs * 4,
                4 * payload.target_count + 200,
            ),
            min_quality=payload.selector.min_quality or 0.0,
        )
        return photos, object_id

    # ------------------------------------------------------------------ #
    @staticmethod
    def _facts(photos: list[Photo]) -> list[PhotoLicenseFacts]:
        return [
            PhotoLicenseFacts(
                photo_id=str(p.id),
                license=p.license,
                allow_derivatives_in_stacks=p.allow_derivatives_in_stacks,
                allow_ai_training=p.allow_ai_training,
                attribution_name=p.attribution_name,
            )
            for p in photos
        ]

    @staticmethod
    def _candidates(photos: list[Photo]) -> list[FrameCandidate]:
        return [
            FrameCandidate(
                photo_id=str(p.id),
                quality_score=p.quality_score if p.quality_score is not None else 0.5,
                dither_x=p.dither_phase_x,
                dither_y=p.dither_phase_y,
                pixel_scale_arcsec=p.pixel_scale_arcsec,
            )
            for p in photos
        ]

    def _estimate_cost(self, pipeline: str, frame_count: int) -> CostEstimateOut:
        per_frame = SECONDS_PER_FRAME.get(pipeline, 3.0)
        seconds = per_frame * frame_count
        return CostEstimateOut(
            compute_seconds=seconds,
            usd=round(seconds * USD_PER_COMPUTE_SECOND, 4),
            basis=(
                f"{per_frame} s/frame medidos en desarrollo para «{pipeline}»; "
                "el coste real lo reporta AWS Batch al terminar."
            ),
        )

    @staticmethod
    def _honest_warnings(
        photos: list[Photo], selection: SelectionResult
    ) -> tuple[list[str], float | None, float | None]:
        """Avisos que impiden prometer lo que la física no da (regla dura 1).

        Devuelve además el límite de difracción de la mejor óptica que entra y la
        escala de placa efectiva estimada.
        """
        chosen = {f.photo_id for f in selection.selected}
        entering = [p for p in photos if str(p.id) in chosen]
        apertures = [p.aperture_mm for p in entering if p.aperture_mm and p.aperture_mm > 0]
        best_limit = diffraction_limit_arcsec(max(apertures)) if apertures else None
        scales = [
            p.pixel_scale_arcsec
            for p in entering
            if p.pixel_scale_arcsec and p.pixel_scale_arcsec > 0
        ]
        finest = min(scales) if scales else None

        warnings: list[str] = []
        if selection.phase_diversity < 0.15:
            warnings.append(
                "La diversidad de dither sub-píxel de esta selección es muy baja: la "
                "combinación ganará SNR y profundidad, pero no recuperará muestreo. "
                "Añade tomas de otros observadores o de otras noches."
            )
        if best_limit is not None and finest is not None and finest < best_limit / 2:
            warnings.append(
                f'Las entradas ya sobremuestrean la óptica ({finest:.2f}"/px frente a '
                f'un límite de difracción de {best_limit:.2f}"). No hay detalle '
                "aliaseado que reclamar; la ganancia será de SNR y rango dinámico."
            )
        if best_limit is not None:
            warnings.append(
                f"Techo físico de resolución angular de esta combinación: "
                f"{best_limit:.2f} arcsec (difracción de la mayor apertura que entra). "
                "Combinar observadores no sintetiza una apertura mayor."
            )
        if len(selection.selected) < 5:
            warnings.append(
                "Con menos de 5 frames la ganancia de SNR es marginal y el rechazo de "
                "artefactos (satélites, rayos cósmicos) es poco fiable."
            )
        # Escala de salida alcanzable: drizzle recupera ~1.5-3x lineal con dither
        # genuino; se usa el extremo conservador, escalado por la diversidad medida.
        estimated_scale = (
            finest / (1.0 + 0.5 * selection.phase_diversity) if finest is not None else None
        )
        return warnings, best_limit, estimated_scale

    # ------------------------------------------------------------------ #
    async def preview(
        self, *, user: User, payload: ReconstructionCreateIn
    ) -> ReconstructionPlanOut:
        """``POST /reconstructions/preview`` — no encola nada.

        Devuelve el plan completo aunque el job esté bloqueado: el punto es que el
        usuario vea **qué** lo bloquea y pueda quitarlo.
        """
        photos, object_id = await self._gather_candidates(payload)
        resolution = resolve_output_license(self._facts(photos))

        target = (
            len(photos)
            if payload.photo_ids is not None
            else min(payload.target_count, self.settings.max_reconstruction_inputs)
        )
        allowed = [p for p in photos if str(p.id) in set(resolution.accepted_photo_ids)]
        selection = select_frames(self._candidates(allowed), max(1, target))
        warnings, best_limit, est_scale = self._honest_warnings(allowed, selection)

        snr_gain = None
        if len(selection.selected) >= 2:
            # Régimen limitado por fondo: SNR ~ sqrt(N) ⇒ ganancia 10·log10(N) dB.
            import math

            snr_gain = round(10.0 * math.log10(len(selection.selected)), 2)

        return ReconstructionPlanOut(
            object_id=object_id,
            pipeline=payload.pipeline,
            input_count=len(selection.selected),
            selected=[
                SelectedFrameOut(
                    photo_id=uuid.UUID(f.photo_id),
                    quality_score=f.quality_score,
                    weight=f.weight,
                    diversity_gain=f.diversity_gain,
                    rank=f.rank,
                )
                for f in selection.selected
            ],
            rejected=[
                RejectedFrameOut(
                    photo_id=uuid.UUID(r.photo_id), reason=r.reason.value, detail=r.detail
                )
                for r in selection.rejected
            ],
            blocked=[BlockedPhotoOut.from_domain(b) for b in resolution.blocked],
            resulting_license=resolution.resulting_license,
            requires_attribution=resolution.requires_attribution,
            license_notes=list(resolution.notes),
            phase_diversity=selection.phase_diversity,
            scale_diversity=selection.scale_diversity,
            best_diffraction_limit_arcsec=best_limit,
            estimated_pixel_scale_arcsec=est_scale,
            estimated_snr_gain_db=snr_gain,
            cost_estimate=self._estimate_cost(payload.pipeline, len(selection.selected)),
            can_run=resolution.ok and len(selection.selected) >= 2,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    async def create(
        self,
        *,
        user: User,
        payload: ReconstructionCreateIn,
        idempotency_key: str | None,
    ) -> tuple[Reconstruction, bool]:
        """``POST /reconstructions``. Devuelve ``(job, created)``.

        ``created=False`` significa que la ``Idempotency-Key`` ya había creado ese
        job: se devuelve el mismo, sin encolar nada nuevo (regla dura 3).
        """
        if idempotency_key:
            existing = await self.reconstructions.get_by_idempotency_key(user.id, idempotency_key)
            if existing is not None:
                return existing, False

        active = await self.reconstructions.count_active_for_user(user.id)
        if active >= self.settings.max_queued_jobs_per_user:
            raise RateLimitError(
                f"Ya tienes {active} trabajos en cola o corriendo; el máximo es "
                f"{self.settings.max_queued_jobs_per_user}.",
                headers={"Retry-After": "60"},
            )
        today = await self.reconstructions.count_last_24h(user.id)
        if today >= self.settings.max_jobs_per_day:
            raise RateLimitError(
                f"Has lanzado {today} trabajos en las últimas 24 h; el máximo diario "
                f"es {self.settings.max_jobs_per_day}.",
                headers={"Retry-After": "3600"},
            )

        photos, object_id = await self._gather_candidates(payload)
        resolution = resolve_output_license(self._facts(photos))
        if not resolution.ok:
            # Regla 1 de docs/licensing.md: 422 con el detalle, sin degradar el job.
            raise LicenseBlockedError(
                "Algunas fotos no permiten obras derivadas. Quítalas de la selección "
                "y vuelve a intentarlo.",
                errors=[
                    {
                        "pointer": "/photo_ids",
                        "detail": b.detail,
                        "code": b.reason.value,
                        "photo_id": b.photo_id,
                    }
                    for b in resolution.blocked
                ],
                extra={"blocked_count": len(resolution.blocked)},
            )

        target = (
            len(photos)
            if payload.photo_ids is not None
            else min(payload.target_count, self.settings.max_reconstruction_inputs)
        )
        selection = select_frames(self._candidates(photos), max(1, target))
        if len(selection.selected) < 2:
            raise UnprocessableError(
                "No hay suficientes frames utilizables: una reconstrucción necesita al menos 2."
            )

        job = Reconstruction(
            requested_by=user.id,
            object_id=object_id,
            pipeline=payload.pipeline,
            pipeline_version=self.pipeline_version,
            model_id=payload.model_id,
            params=dict(payload.params),
            status=JobStatus.QUEUED,
            input_count=len(selection.selected),
            license=resolution.resulting_license,
            idempotency_key=idempotency_key,
            is_public=payload.is_public,
        )
        await self.reconstructions.add(job)

        by_id = {str(p.id): p for p in photos}
        rows: list[ReconstructionInput] = [
            ReconstructionInput(
                reconstruction_id=job.id,
                photo_id=uuid.UUID(f.photo_id),
                weight=f.weight,
                was_rejected=False,
                snapshot_license=by_id[f.photo_id].license,
                snapshot_attribution_name=by_id[f.photo_id].attribution_name,
            )
            for f in selection.selected
        ]
        rows += [
            ReconstructionInput(
                reconstruction_id=job.id,
                photo_id=uuid.UUID(r.photo_id),
                weight=0.0,
                was_rejected=True,
                rejection_reason=f"{r.reason.value}: {r.detail}",
                snapshot_license=by_id[r.photo_id].license,
                snapshot_attribution_name=by_id[r.photo_id].attribution_name,
            )
            for r in selection.rejected
            if r.photo_id in by_id
        ]
        # Procedencia primero, cola después: si el encolado falla, la fila queda y
        # el job se reintenta; al revés se perdería la trazabilidad.
        await self.reconstructions.add_inputs(rows)

        await self.audit.record(
            action="reconstruction.created",
            entity_type="reconstruction",
            entity_id=job.id,
            actor_id=user.id,
            payload={
                "pipeline": job.pipeline,
                "input_count": job.input_count,
                "license": job.license.value if job.license else None,
            },
        )
        await self.queue.send(
            self.settings.sqs_queue_reconstruct,
            {
                "type": "reconstruct",
                "reconstruction_id": str(job.id),
                "pipeline": job.pipeline,
                "params": job.params,
                "model_id": str(job.model_id) if job.model_id else None,
                "photo_ids": [f.photo_id for f in selection.selected],
                "weights": [f.weight for f in selection.selected],
                "license": job.license.value if job.license else None,
                "enqueued_at": datetime.now(UTC).isoformat(),
            },
            idempotency_key=idempotency_key or f"recon:{job.id}",
        )
        log.info("reconstruction_enqueued", reconstruction_id=str(job.id))
        return job, True

    # ------------------------------------------------------------------ #
    async def cancel(self, *, reconstruction_id: uuid.UUID, user: User) -> Reconstruction:
        """``DELETE /reconstructions/{id}`` — cancela si está en cola o corriendo."""
        job = await self.reconstructions.get(reconstruction_id)
        if job is None:
            raise NotFoundError("La reconstrucción no existe.")
        if job.requested_by != user.id:
            raise ForbiddenError("Solo quien la pidió puede cancelarla.")
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise ConflictError(
                f"La reconstrucción está en estado «{job.status.value}»; ya no se puede cancelar."
            )
        job.status = JobStatus.CANCELLED
        job.finished_at = datetime.now(UTC)
        await self.audit.record(
            action="reconstruction.cancelled",
            entity_type="reconstruction",
            entity_id=job.id,
            actor_id=user.id,
        )
        return job
