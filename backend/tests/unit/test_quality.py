"""Tests de ``app.domain.quality``: rango, determinismo y monotonía por componente."""

from __future__ import annotations

import pytest

from app.domain.quality import (
    DEFAULT_WEIGHTS,
    UNKNOWN_SCORE,
    QualityWeights,
    quality_score,
)

#: Un frame "medio" del que partir para mover un solo eje a la vez.
BASE = {
    "fwhm_arcsec": 3.0,
    "eccentricity": 0.2,
    "snr_estimate": 30.0,
    "star_count": 400,
    "airmass": 1.4,
    "moon_illumination": 0.3,
    "moon_separation_deg": 70.0,
    "bortle_estimate": 5,
}


def score(**overrides: object) -> float:
    return quality_score(**{**BASE, **overrides}).score  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Contrato básico
# --------------------------------------------------------------------------- #
def test_weights_sum_to_one() -> None:
    assert DEFAULT_WEIGHTS.total() == pytest.approx(1.0)


def test_score_is_in_range() -> None:
    assert 0.0 <= score() <= 1.0


def test_score_is_deterministic() -> None:
    assert score() == score()


def test_perfect_frame_scores_near_one() -> None:
    value = score(
        fwhm_arcsec=1.0,
        eccentricity=0.0,
        snr_estimate=200.0,
        star_count=5000,
        airmass=1.0,
        moon_illumination=0.0,
        moon_separation_deg=180.0,
        bortle_estimate=1,
    )
    assert value > 0.95


def test_terrible_frame_scores_near_zero() -> None:
    value = score(
        fwhm_arcsec=12.0,
        eccentricity=0.9,
        snr_estimate=1.0,
        star_count=0,
        airmass=6.0,
        moon_illumination=1.0,
        moon_separation_deg=5.0,
        bortle_estimate=9,
    )
    assert value < 0.05


def test_extreme_inputs_never_leave_the_unit_interval() -> None:
    assert 0.0 <= score(fwhm_arcsec=1e6, snr_estimate=1e9, star_count=10**9) <= 1.0
    assert 0.0 <= score(eccentricity=-5.0, airmass=float("inf")) <= 1.0


# --------------------------------------------------------------------------- #
# Monotonía, componente a componente
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("field", "values", "improves"),
    [
        ("fwhm_arcsec", [1.5, 2.5, 4.0, 6.0, 8.0], False),
        ("eccentricity", [0.0, 0.1, 0.3, 0.5, 0.6], False),
        ("snr_estimate", [3.0, 10.0, 30.0, 60.0, 100.0], True),
        ("star_count", [0, 10, 100, 800, 2000], True),
        ("airmass", [1.0, 1.5, 2.0, 3.0, 4.5], False),
        ("moon_illumination", [0.0, 0.25, 0.5, 0.75, 1.0], False),
        ("moon_separation_deg", [15.0, 30.0, 50.0, 70.0, 90.0], True),
        ("bortle_estimate", [1, 3, 5, 7, 9], False),
    ],
)
def test_score_is_monotone_in_each_component(
    field: str, values: list[float], improves: bool
) -> None:
    """Mejorar un eje nunca baja la puntuación, y empeorarlo nunca la sube."""
    scores = [score(**{field: v}) for v in values]
    expected = sorted(scores) if improves else sorted(scores, reverse=True)
    assert scores == expected


def test_moon_only_matters_when_it_is_lit() -> None:
    """Luna nueva a 20° no penaliza; luna llena a 20° sí."""
    new_moon_close = score(moon_illumination=0.0, moon_separation_deg=20.0)
    new_moon_far = score(moon_illumination=0.0, moon_separation_deg=150.0)
    full_moon_close = score(moon_illumination=1.0, moon_separation_deg=20.0)
    assert new_moon_close == pytest.approx(new_moon_far)
    assert full_moon_close < new_moon_close


def test_unknown_moon_separation_assumes_the_worst_case() -> None:
    """Sin separación conocida se penaliza como si la Luna estuviera encima."""
    unknown = score(moon_illumination=0.8, moon_separation_deg=None)
    far = score(moon_illumination=0.8, moon_separation_deg=150.0)
    assert unknown < far


# --------------------------------------------------------------------------- #
# Datos ausentes
# --------------------------------------------------------------------------- #
def test_no_data_at_all_returns_the_neutral_score() -> None:
    result = quality_score()
    assert result.score == UNKNOWN_SCORE
    assert result.components == {}
    assert len(result.missing) == 7


def test_missing_fields_are_excluded_not_penalised() -> None:
    """Una foto sin astrometría no debe puntuar peor por ignorancia."""
    full = quality_score(fwhm_arcsec=1.5, snr_estimate=200.0, airmass=1.0)
    assert full.score > 0.9
    assert set(full.weights_used) == {"fwhm", "snr", "airmass"}
    assert sum(full.weights_used.values()) == pytest.approx(1.0)


def test_breakdown_explains_the_score() -> None:
    result = quality_score(**BASE)  # type: ignore[arg-type]
    assert set(result.components) == {
        "fwhm",
        "eccentricity",
        "snr",
        "star_count",
        "airmass",
        "moon",
        "bortle",
    }
    recomputed = sum(result.components[k] * result.weights_used[k] for k in result.components)
    assert recomputed == pytest.approx(result.score)


def test_custom_weights_shift_the_emphasis() -> None:
    """Con todo el peso en la FWHM, el resto de ejes deja de importar."""
    only_fwhm = QualityWeights(
        fwhm=1.0,
        snr=0.0,
        airmass=0.0,
        eccentricity=0.0,
        moon=0.0,
        star_count=0.0,
        bortle=0.0,
    )
    good = quality_score(fwhm_arcsec=1.5, snr_estimate=3.0, weights=only_fwhm).score
    bad = quality_score(fwhm_arcsec=1.5, snr_estimate=200.0, weights=only_fwhm).score
    assert good == pytest.approx(bad)
