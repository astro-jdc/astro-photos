"""Schemas de reconstrucciones: preview, creación, estado, procedencia y resultado."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.licensing import LicenseCode
from app.models.enums import JobStatus
from app.schemas.common import Schema
from app.schemas.license import BlockedPhotoOut
from app.schemas.search import PhotoSearchQuery

__all__ = [
    "BestSingleFrameOut",
    "ReconstructionCreateIn",
    "ReconstructionEvent",
    "ReconstructionInputOut",
    "ReconstructionOut",
    "ReconstructionPlanOut",
    "ReconstructionResultOut",
    "SelectedFrameOut",
]

#: Pipelines soportados hoy. ``docs/data-model.md``: ``classical-stack-v1``,
#: ``drizzle-v1``, ``burst-sr-v1``. Se valida contra esta lista para que un typo no
#: encole un job que nadie va a consumir.
KNOWN_PIPELINES: frozenset[str] = frozenset({"classical-stack-v1", "drizzle-v1", "burst-sr-v1"})


class ReconstructionCreateIn(Schema):
    """Cuerpo de ``POST /reconstructions`` **y** de ``POST /reconstructions/preview``.

    O das ``photo_ids`` explícitos, o das un ``selector`` con la sintaxis de la
    búsqueda y el backend elige los mejores N frames.
    """

    object_id: UUID | None = None
    photo_ids: list[UUID] | None = Field(default=None, max_length=500)
    selector: PhotoSearchQuery | None = None
    pipeline: str = "classical-stack-v1"
    params: dict[str, Any] = Field(default_factory=dict)
    model_id: UUID | None = None
    #: Cuántos frames quiere el usuario cuando usa ``selector``.
    target_count: int = Field(default=50, ge=1, le=500)
    is_public: bool = True

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.photo_ids is None) == (self.selector is None):
            raise ValueError(
                "Da exactamente uno: `photo_ids` explícitos o un `selector` de búsqueda."
            )
        if self.photo_ids is not None and len(self.photo_ids) < 2:
            raise ValueError(
                "Una reconstrucción necesita al menos 2 frames; con uno no hay nada que combinar."
            )
        if self.photo_ids is not None and len(set(self.photo_ids)) != len(self.photo_ids):
            raise ValueError("`photo_ids` contiene duplicados.")
        if self.selector is not None and self.object_id is None and not self.selector.object:
            raise ValueError(
                "Con `selector` hay que fijar el objetivo: `object_id` o `selector.object`."
            )
        if self.pipeline not in KNOWN_PIPELINES:
            raise ValueError(
                f"Pipeline desconocido: {self.pipeline}. Disponibles: "
                + ", ".join(sorted(KNOWN_PIPELINES))
            )
        return self


class SelectedFrameOut(Schema):
    photo_id: UUID
    quality_score: float
    weight: float
    diversity_gain: float
    rank: int


class RejectedFrameOut(Schema):
    photo_id: UUID
    reason: str
    detail: str


class CostEstimateOut(Schema):
    """Estimación grosera; el número real sale de AWS Batch al terminar."""

    compute_seconds: float
    usd: float
    #: Cómo se estimó, para que el frontend no lo presente como una promesa.
    basis: str


class ReconstructionPlanOut(Schema):
    """``POST /reconstructions/preview`` — **no encola nada**.

    Devuelve el plan completo: qué frames entran y con qué peso, qué se descarta y
    por qué, la licencia resultante, las fotos que bloquean el job y el coste
    estimado. El frontend siempre llama aquí primero.
    """

    object_id: UUID | None = None
    pipeline: str
    input_count: int
    selected: list[SelectedFrameOut]
    rejected: list[RejectedFrameOut]
    blocked: list[BlockedPhotoOut]
    resulting_license: LicenseCode | None
    requires_attribution: bool = True
    license_notes: list[str] = Field(default_factory=list)
    #: Diversidad de fase sub-píxel de la selección, 0–1. Es lo que habilita la
    #: recuperación de muestreo; un valor bajo significa "más SNR, no más detalle".
    phase_diversity: float = 0.0
    scale_diversity: float = 0.0
    #: Techo físico de resolución angular: difracción de la mejor óptica que entra.
    best_diffraction_limit_arcsec: float | None = None
    #: Escala de placa efectiva alcanzable, arcsec/píxel.
    estimated_pixel_scale_arcsec: float | None = None
    estimated_snr_gain_db: float | None = None
    cost_estimate: CostEstimateOut | None = None
    can_run: bool = False
    #: Avisos honestos (regla dura 1 de CLAUDE.md).
    warnings: list[str] = Field(default_factory=list)


class ReconstructionOut(Schema):
    """``GET /reconstructions/{id}``."""

    id: UUID
    requested_by: UUID
    object_id: UUID | None = None
    pipeline: str
    pipeline_version: str
    model_id: UUID | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus
    progress: float = 0.0
    input_count: int = 0
    license: LicenseCode | None = None
    metrics: dict[str, Any] | None = None
    error_message: str | None = None
    preview_url: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    compute_seconds: float | None = None
    cost_usd_estimate: float | None = None
    is_public: bool = True
    created_at: datetime


class ReconstructionInputOut(Schema):
    """``GET /reconstructions/{id}/inputs`` — procedencia."""

    photo_id: UUID
    weight: float
    was_rejected: bool
    rejection_reason: str | None = None
    alignment_rms_px: float | None = None
    snapshot_license: LicenseCode
    snapshot_attribution_name: str | None = None


class BestSingleFrameOut(Schema):
    """El mejor frame individual de las entradas.

    No es un extra: es la **comparación honesta** (``docs/api.md``). Sin ella la
    interfaz afirma una mejora que no enseña, y el usuario no puede juzgar si la
    reconstrucción aportó algo sobre la mejor toma que ya existía.
    """

    photo_id: UUID
    preview_url: str | None = None
    fwhm_arcsec: float | None = None
    snr_estimate: float | None = None
    quality_score: float | None = None


class ReconstructionResultOut(Schema):
    """``GET /reconstructions/{id}/result`` 🔓 — URLs firmadas."""

    reconstruction_id: UUID
    status: JobStatus
    license: LicenseCode | None = None
    pipeline: str
    pipeline_version: str
    model_id: UUID | None = None

    result_url: str | None = None
    preview_url: str | None = None
    #: Regla dura 2 de ``CLAUDE.md``: toda salida de un modelo aprendido lleva mapa
    #: de incertidumbre. En astronomía una fuente alucinada es un falso
    #: descubrimiento, no un defecto estético.
    uncertainty_map_url: str | None = None
    weight_map_url: str | None = None
    provenance_json_url: str | None = None
    attribution_md_url: str | None = None
    report_url: str | None = None

    #: ``None`` mientras el job no ha terminado.
    best_single_frame: BestSingleFrameOut | None = None
    metrics: dict[str, Any] | None = None
    expires_at: datetime | None = None
    #: Créditos ya renderizados, por si el cliente no quiere bajar el .md.
    attribution_markdown: str | None = None


class ReconstructionEvent(Schema):
    """Un evento del SSE de ``GET /reconstructions/{id}/events``."""

    reconstruction_id: UUID
    status: JobStatus
    progress: float
    #: Etapa del pipeline (`align`, `coadd`, `deconv`…) que reporta el worker.
    stage: str | None = None
    message: str | None = None
    metrics: dict[str, Any] | None = None
    at: datetime
