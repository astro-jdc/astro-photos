"""Puntuación de calidad de un frame: función pura, determinista y explicable.

``photos.quality_score`` es un escalar 0–1 que ordena la selección de frames. No
pretende ser una medida física: es una **utilidad** que combina lo que sabemos de
una toma en un número comparable entre observadores distintos.

Decisiones de diseño:

* Media aritmética ponderada de sub-puntuaciones, cada una en [0, 1]. Se eligió
  frente a una media geométrica porque queremos **monotonía suave**: un solo
  factor malo degrada, pero no anula, un frame que aporta señal. El rechazo duro
  (satélites, saturación, nubes) es trabajo del worker de QA, no de este escalar.
* Los pesos son constantes con nombre, no números mágicos, y suman 1.0.
* Los campos desconocidos (``None``) **no se penalizan**: se excluyen y los pesos
  se renormalizan sobre lo disponible. Penalizar la ignorancia castigaría a las
  fotos aún no procesadas por el worker de astrometría.
* Toda la sub-puntuación está acotada a [0, 1] antes de ponderar, de modo que
  ``quality_score`` nunca se sale de rango por un valor extremo de entrada.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "UNKNOWN_SCORE",
    "QualityBreakdown",
    "QualityWeights",
    "quality_score",
]

# --------------------------------------------------------------------------- #
# Umbrales de cada sub-puntuación (documentados uno a uno)
# --------------------------------------------------------------------------- #

#: FWHM por debajo de la cual una toma amateur ya es excelente (arcsec).
FWHM_EXCELLENT_ARCSEC = 1.5
#: FWHM a partir de la cual la toma aporta poco detalle (arcsec).
FWHM_POOR_ARCSEC = 8.0

#: Elipticidad a partir de la cual las estrellas están claramente arrastradas.
ECC_POOR = 0.6

#: SNR por debajo de la cual el frame es ruido (relación señal/ruido estimada).
SNR_FLOOR = 3.0
#: SNR que ya se considera plena.
SNR_REFERENCE = 100.0

#: Nº de estrellas detectadas que se considera un campo plenamente resuelto.
STAR_COUNT_REFERENCE = 2000

#: Coeficiente de extinción efectivo por masa de aire (mag/airmass en V, sitio decente).
#: Se usa como exponente de transmisión: score = exp(-K·(X-1)).
EXTINCTION_PER_AIRMASS = 0.35
#: Masa de aire a partir de la cual el frame se considera inservible.
AIRMASS_CUTOFF = 5.0

#: Separación a la Luna a partir de la cual su contribución al fondo es despreciable.
MOON_SAFE_SEPARATION_DEG = 90.0
#: Separación por debajo de la cual la Luna domina el fondo de cielo.
MOON_BAD_SEPARATION_DEG = 15.0

#: Clase Bortle mínima y máxima de la escala.
BORTLE_BEST = 1
BORTLE_WORST = 9

#: Valor devuelto cuando no se conoce ninguna métrica. Neutro a propósito: una foto
#: sin procesar no debe adelantar ni retrasar posiciones frente a las medidas.
UNKNOWN_SCORE = 0.5


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True, slots=True)
class QualityWeights:
    """Pesos de cada componente. Suman 1.0; se renormalizan si faltan datos.

    La jerarquía refleja lo que dice ``docs/research/multi-image-astro-reconstruction.md``:
    la nitidez medida (FWHM) es el criterio de *lucky imaging* y manda; la SNR es la
    ganancia garantizada al apilar; la masa de aire corrompe fotometría y PSF; la
    Luna y el Bortle son fondo aditivo, modelable y por tanto menos letal.
    """

    fwhm: float = 0.30
    snr: float = 0.22
    airmass: float = 0.12
    eccentricity: float = 0.10
    moon: float = 0.10
    star_count: float = 0.08
    bortle: float = 0.08

    def total(self) -> float:
        return (
            self.fwhm
            + self.snr
            + self.airmass
            + self.eccentricity
            + self.moon
            + self.star_count
            + self.bortle
        )


DEFAULT_WEIGHTS = QualityWeights()


@dataclass(frozen=True, slots=True)
class QualityBreakdown:
    """Desglose de la puntuación, para poder explicarla en la UI y en los tests."""

    score: float
    components: dict[str, float]
    weights_used: dict[str, float]
    missing: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Sub-puntuaciones
# --------------------------------------------------------------------------- #
def _fwhm_subscore(fwhm_arcsec: float) -> float:
    """1.0 en seeing excelente → 0.0 en ``FWHM_POOR_ARCSEC``. Lineal en arcsec."""
    span = FWHM_POOR_ARCSEC - FWHM_EXCELLENT_ARCSEC
    return _clamp((FWHM_POOR_ARCSEC - fwhm_arcsec) / span)


def _eccentricity_subscore(eccentricity: float) -> float:
    """1.0 con estrellas redondas → 0.0 en ``ECC_POOR`` (arrastre evidente)."""
    return _clamp(1.0 - eccentricity / ECC_POOR)


def _snr_subscore(snr: float) -> float:
    """Logarítmica: pasar de 3 a 10 vale más que pasar de 90 a 100."""
    if snr <= SNR_FLOOR:
        return 0.0
    return _clamp(math.log10(snr / SNR_FLOOR) / math.log10(SNR_REFERENCE / SNR_FLOOR))


def _star_count_subscore(star_count: int) -> float:
    """Logarítmica: proxy de profundidad y de si la astrometría tendrá anclajes."""
    if star_count <= 0:
        return 0.0
    return _clamp(math.log10(1.0 + star_count) / math.log10(1.0 + STAR_COUNT_REFERENCE))


def _airmass_subscore(airmass: float) -> float:
    """Transmisión relativa al cenit, ``exp(-K·(X-1))``, y 0 por encima del corte.

    A X=2 da 0.70, a X=3 da 0.49: no es un rechazo, es una penalización que refleja
    tanto la extinción como la dispersión cromática diferencial.
    """
    if not math.isfinite(airmass) or airmass >= AIRMASS_CUTOFF:
        return 0.0
    x = max(1.0, airmass)
    return _clamp(math.exp(-EXTINCTION_PER_AIRMASS * (x - 1.0)))


def _moon_subscore(illumination: float, separation_deg: float | None) -> float:
    """Penaliza el producto (fracción iluminada) × (proximidad al objetivo).

    Luna nueva no molesta aunque esté encima; luna llena a 120° tampoco. Si no se
    conoce la separación se asume el peor caso compatible con la iluminación, que
    es lo conservador para una foto todavía sin resolver astrométricamente.
    """
    illum = _clamp(illumination)
    if separation_deg is None:
        proximity = 1.0
    else:
        span = MOON_SAFE_SEPARATION_DEG - MOON_BAD_SEPARATION_DEG
        proximity = _clamp((MOON_SAFE_SEPARATION_DEG - separation_deg) / span)
    return _clamp(1.0 - illum * proximity)


def _bortle_subscore(bortle: int) -> float:
    """1.0 en cielo Bortle 1 → 0.0 en Bortle 9. Lineal en la escala."""
    span = BORTLE_WORST - BORTLE_BEST
    return _clamp((BORTLE_WORST - bortle) / span)


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def quality_score(
    *,
    fwhm_arcsec: float | None = None,
    eccentricity: float | None = None,
    snr_estimate: float | None = None,
    star_count: int | None = None,
    airmass: float | None = None,
    moon_illumination: float | None = None,
    moon_separation_deg: float | None = None,
    bortle_estimate: int | None = None,
    weights: QualityWeights = DEFAULT_WEIGHTS,
) -> QualityBreakdown:
    """Calidad agregada 0–1 de un frame, con su desglose.

    Todos los argumentos son keyword-only a propósito: siete floats posicionales
    serían una fuente garantizada de bugs silenciosos.

    Es **monótona** en cada componente por separado: bajar la FWHM, subir la SNR,
    bajar la masa de aire, bajar la elipticidad, alejarse de la Luna o mejorar el
    Bortle nunca puede bajar la puntuación.
    """
    components: dict[str, float] = {}
    used: dict[str, float] = {}
    missing: list[str] = []

    def put(name: str, value: float | None, weight: float, sub: float) -> None:
        if value is None:
            missing.append(name)
            return
        components[name] = sub
        used[name] = weight

    put("fwhm", fwhm_arcsec, weights.fwhm, _fwhm_subscore(fwhm_arcsec or 0.0))
    put(
        "eccentricity",
        eccentricity,
        weights.eccentricity,
        _eccentricity_subscore(eccentricity or 0.0),
    )
    put("snr", snr_estimate, weights.snr, _snr_subscore(snr_estimate or 0.0))
    put(
        "star_count",
        None if star_count is None else float(star_count),
        weights.star_count,
        _star_count_subscore(star_count or 0),
    )
    put("airmass", airmass, weights.airmass, _airmass_subscore(airmass or 1.0))
    put(
        "moon",
        moon_illumination,
        weights.moon,
        _moon_subscore(moon_illumination or 0.0, moon_separation_deg),
    )
    put(
        "bortle",
        None if bortle_estimate is None else float(bortle_estimate),
        weights.bortle,
        _bortle_subscore(bortle_estimate or BORTLE_BEST),
    )

    total_weight = sum(used.values())
    if total_weight <= 0.0:
        return QualityBreakdown(
            score=UNKNOWN_SCORE,
            components={},
            weights_used={},
            missing=tuple(missing),
        )

    score = sum(components[k] * used[k] for k in components) / total_weight
    return QualityBreakdown(
        score=_clamp(score),
        components=components,
        weights_used={k: v / total_weight for k, v in used.items()},
        missing=tuple(missing),
    )
