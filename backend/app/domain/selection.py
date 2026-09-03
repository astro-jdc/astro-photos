"""Selección de frames para una reconstrucción: calidad **y** diversidad.

Por qué no basta con "coger los N mejores":

``docs/research/multi-image-astro-reconstruction.md`` separa cuatro ganancias, y la
segunda —*sampling*, recuperar las frecuencias espaciales que cada frame aliasea
porque su rejilla de píxeles es demasiado gruesa— **solo existe si las entradas
tienen fases sub-píxel distintas**. Drizzle y la fusión multi-frame reconstruyen
esas frecuencias a partir de muestras irregularmente distribuidas; cien frames con
idéntico dither son, para el muestreo, un solo frame con más SNR. La diversidad de
escala de placa juega el mismo papel a otra frecuencia: mezclar focales distintas
distribuye las muestras sobre la rejilla de salida.

Así que este módulo maximiza calidad **sujeto a** cubrir el espacio de diversidad,
con un greedy tipo MMR (*maximal marginal relevance*): en cada paso elige el
candidato que maximiza ``λ·calidad + (1-λ)·distancia al conjunto ya elegido``.

Es puro y **determinista**: mismos candidatos ⇒ misma selección, y todos los
empates se rompen por ``photo_id`` ascendente (regla dura 3 de ``CLAUDE.md``:
nada de depender del orden del sistema de ficheros ni de un ``set`` de Python).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "FrameCandidate",
    "RejectedFrame",
    "RejectionReason",
    "SelectedFrame",
    "SelectionResult",
    "select_frames",
]

#: Peso de la calidad frente a la diversidad en el criterio MMR.
#: 0.65 se eligió para que un frame excelente pero redundante siga entrando antes
#: que uno mediocre y original, pero no arrase con toda la selección.
DEFAULT_QUALITY_WEIGHT = 0.65

#: Reparto de la distancia de diversidad entre fase sub-píxel y escala de placa.
#: La fase manda porque es la que habilita la recuperación de muestreo; la escala
#: aporta, pero mezclar focales muy distintas también complica la reproyección.
DEFAULT_PHASE_WEIGHT = 0.7
DEFAULT_SCALE_WEIGHT = 0.3

#: Rango de escalas (factor multiplicativo) sobre el que la distancia de escala
#: satura en 1.0. Un factor 4 (p. ej. 2"/px frente a 8"/px) ya es diversidad plena.
SCALE_SPAN = 4.0

#: Distancia máxima posible en el toro de fase [0,1)²: sqrt(0.5² + 0.5²).
_MAX_PHASE_DISTANCE = math.sqrt(0.5)


class RejectionReason(StrEnum):
    """Motivos de descarte. Se persisten en ``reconstruction_inputs.rejection_reason``."""

    BELOW_MIN_QUALITY = "below_min_quality"
    MISSING_GEOMETRY = "missing_geometry"
    NOT_SELECTED = "not_selected"


@dataclass(frozen=True, slots=True)
class FrameCandidate:
    """Un frame candidato, reducido a lo que la selección necesita saber.

    ``dither_x`` / ``dither_y`` son la **fase sub-píxel** del frame respecto de la
    rejilla de salida común: la parte fraccionaria de la posición del origen del
    frame reproyectado, en [0, 1). La calcula el worker de astrometría a partir del
    WCS; aquí solo se consume.
    """

    photo_id: str
    quality_score: float
    #: Fase sub-píxel en x, [0, 1). ``None`` si la foto no está resuelta.
    dither_x: float | None = None
    #: Fase sub-píxel en y, [0, 1).
    dither_y: float | None = None
    #: Escala de placa en arcsec/píxel. ``None`` si no se conoce.
    pixel_scale_arcsec: float | None = None

    @property
    def has_geometry(self) -> bool:
        return (
            self.dither_x is not None
            and self.dither_y is not None
            and self.pixel_scale_arcsec is not None
            and self.pixel_scale_arcsec > 0.0
        )


@dataclass(frozen=True, slots=True)
class SelectedFrame:
    """Un frame elegido, con el peso con el que entrará en ``reconstruction_inputs``."""

    photo_id: str
    quality_score: float
    #: Contribución efectiva normalizada, 0–1. Suma 1.0 sobre la selección.
    weight: float
    #: Distancia mínima en el espacio de diversidad al resto de elegidos, 0–1.
    diversity_gain: float
    #: Orden de elección (0 = frame de referencia).
    rank: int


@dataclass(frozen=True, slots=True)
class RejectedFrame:
    """Un candidato descartado, con el motivo que se guarda como procedencia."""

    photo_id: str
    reason: RejectionReason
    detail: str


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Plan de selección. Alimenta el `preview` y la creación de la reconstrucción."""

    selected: tuple[SelectedFrame, ...]
    rejected: tuple[RejectedFrame, ...]
    #: Diversidad de fase efectiva 0–1 (cobertura del toro sub-píxel).
    phase_diversity: float
    #: Diversidad de escala efectiva 0–1.
    scale_diversity: float

    @property
    def input_count(self) -> int:
        return len(self.selected)


def _toroidal_delta(a: float, b: float) -> float:
    """Distancia en el círculo unidad: la fase 0.99 y la 0.01 distan 0.02, no 0.98."""
    d = abs((a % 1.0) - (b % 1.0))
    return min(d, 1.0 - d)


def _phase_distance(a: FrameCandidate, b: FrameCandidate) -> float:
    """Distancia de fase sub-píxel normalizada a [0, 1]. 0.0 si falta la geometría."""
    if a.dither_x is None or a.dither_y is None or b.dither_x is None or b.dither_y is None:
        return 0.0
    dx = _toroidal_delta(a.dither_x, b.dither_x)
    dy = _toroidal_delta(a.dither_y, b.dither_y)
    return min(1.0, math.hypot(dx, dy) / _MAX_PHASE_DISTANCE)


def _scale_distance(a: FrameCandidate, b: FrameCandidate) -> float:
    """Distancia de escala en octavas, normalizada y saturada en ``SCALE_SPAN``."""
    sa, sb = a.pixel_scale_arcsec, b.pixel_scale_arcsec
    if sa is None or sb is None or sa <= 0.0 or sb <= 0.0:
        return 0.0
    octaves = abs(math.log2(sa / sb))
    return min(1.0, octaves / math.log2(SCALE_SPAN))


def _diversity_distance(
    a: FrameCandidate, b: FrameCandidate, phase_weight: float, scale_weight: float
) -> float:
    return phase_weight * _phase_distance(a, b) + scale_weight * _scale_distance(a, b)


def select_frames(
    candidates: Sequence[FrameCandidate],
    target_count: int,
    *,
    min_quality: float = 0.0,
    quality_weight: float = DEFAULT_QUALITY_WEIGHT,
    phase_weight: float = DEFAULT_PHASE_WEIGHT,
    scale_weight: float = DEFAULT_SCALE_WEIGHT,
) -> SelectionResult:
    """Elige los mejores ``target_count`` frames maximizando calidad y diversidad.

    Algoritmo:

    1. Descarta los candidatos por debajo de ``min_quality`` y los que no tienen
       geometría (sin fase sub-píxel no se puede razonar sobre muestreo; entran
       igualmente si no queda ninguno con geometría, ver más abajo).
    2. Elige como referencia el de mayor calidad (empates por ``photo_id``).
    3. Repite: elige el candidato que maximiza
       ``quality_weight·calidad + (1-quality_weight)·d_min(candidato, elegidos)``,
       donde ``d_min`` es la distancia mínima en el espacio (fase sub-píxel ⊕
       escala) a los ya elegidos.
    4. Los pesos de salida son proporcionales a la calidad, normalizados a suma 1.

    El peso devuelto es un **placeholder de producto**: la ponderación correcta para
    la coadición es la de Zackay & Ofek (filtro adaptado con la PSF propia de cada
    frame, peso ∝ transparencia/varianza) y vive en ``models/``, con acceso a los
    píxeles. Aquí solo se ordena y se reparte crédito de autoría.

    ``target_count`` mayor que el número de candidatos válidos devuelve todos.
    """
    if target_count <= 0:
        raise ValueError("target_count debe ser >= 1")
    if not 0.0 <= quality_weight <= 1.0:
        raise ValueError("quality_weight debe estar en [0, 1]")

    rejected: list[RejectedFrame] = []
    pool: list[FrameCandidate] = []
    no_geometry: list[FrameCandidate] = []

    for c in sorted(candidates, key=lambda c: c.photo_id):
        if c.quality_score < min_quality:
            rejected.append(
                RejectedFrame(
                    c.photo_id,
                    RejectionReason.BELOW_MIN_QUALITY,
                    f"quality_score {c.quality_score:.3f} < mínimo {min_quality:.3f}",
                )
            )
        elif c.has_geometry:
            pool.append(c)
        else:
            no_geometry.append(c)

    # Sin geometría no hay diversidad que medir, pero un frame sin resolver aún puede
    # aportar SNR. Solo se usan si no hay suficientes frames con geometría.
    if len(pool) < target_count and no_geometry:
        pool.extend(no_geometry)
    else:
        rejected.extend(
            RejectedFrame(
                c.photo_id,
                RejectionReason.MISSING_GEOMETRY,
                "sin fase sub-píxel ni escala de placa (falta plate solving)",
            )
            for c in no_geometry
        )

    if not pool:
        return SelectionResult((), tuple(rejected), 0.0, 0.0)

    diversity_weight = 1.0 - quality_weight
    w_sum = phase_weight + scale_weight
    p_w = phase_weight / w_sum if w_sum > 0 else 0.5
    s_w = scale_weight / w_sum if w_sum > 0 else 0.5

    def distance(a: FrameCandidate, b: FrameCandidate) -> float:
        # Con fallback activo puede haber candidatos sin geometría: su distancia al
        # conjunto es 0 (no aportan diversidad demostrable, solo señal).
        if not (a.has_geometry and b.has_geometry):
            return 0.0
        return _diversity_distance(a, b, p_w, s_w)

    remaining = list(pool)
    # Semilla: mejor calidad, empates por photo_id (remaining ya viene ordenado por id).
    remaining.sort(key=lambda c: (-c.quality_score, c.photo_id))
    chosen: list[tuple[FrameCandidate, float]] = [(remaining.pop(0), 1.0)]

    while remaining and len(chosen) < target_count:
        best_index = 0
        best_key: tuple[float, str] | None = None
        for i, cand in enumerate(remaining):
            d_min = min(distance(cand, sel) for sel, _ in chosen)
            score = quality_weight * cand.quality_score + diversity_weight * d_min
            # Máximo por score, empates por photo_id ascendente ⇒ clave (-score, id).
            key = (-score, cand.photo_id)
            if best_key is None or key < best_key:
                best_key, best_index = key, i
        cand = remaining.pop(best_index)
        d_min = min(distance(cand, sel) for sel, _ in chosen)
        chosen.append((cand, d_min))

    for cand in remaining:
        rejected.append(
            RejectedFrame(
                cand.photo_id,
                RejectionReason.NOT_SELECTED,
                f"no entró en los {target_count} mejores por calidad y diversidad",
            )
        )

    quality_total = sum(c.quality_score for c, _ in chosen)
    n = len(chosen)
    selected = tuple(
        SelectedFrame(
            photo_id=c.photo_id,
            quality_score=c.quality_score,
            weight=(c.quality_score / quality_total) if quality_total > 0 else 1.0 / n,
            diversity_gain=d,
            rank=rank,
        )
        for rank, (c, d) in enumerate(chosen)
    )

    geo = [c for c, _ in chosen if c.has_geometry]
    phase_div = _mean_pairwise(geo, _phase_distance)
    scale_div = _mean_pairwise(geo, _scale_distance)
    return SelectionResult(
        selected=selected,
        rejected=tuple(sorted(rejected, key=lambda r: r.photo_id)),
        phase_diversity=phase_div,
        scale_diversity=scale_div,
    )


def _mean_pairwise(
    items: Sequence[FrameCandidate],
    metric: Callable[[FrameCandidate, FrameCandidate], float],
) -> float:
    """Media de la métrica sobre todos los pares. 0.0 con menos de dos elementos."""
    if len(items) < 2:
        return 0.0
    total = 0.0
    count = 0
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            total += metric(a, b)
            count += 1
    return total / count if count else 0.0
