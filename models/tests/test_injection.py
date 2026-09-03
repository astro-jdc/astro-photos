"""Injected sources must be recovered linearly in flux.

Rule 4 of the astro-ml brief. The recovery curve is the evidence that the
pipeline measures rather than invents: a slope of 1 means what comes out is
what went in; a slope above 1, or a positive intercept, means the pipeline is
manufacturing flux, and in astronomy manufactured flux is a false discovery.

The experiment injects point sources of known flux at known positions into the
individual frames — each convolved with **that frame's own PSF**, so a
bad-seeing frame receives a bad-seeing star, exactly as a real source would
appear — then stacks and measures.
"""

from __future__ import annotations

import numpy as np
import pytest

from astrostack.metrics.injection import (
    inject_into_frames,
    injection_experiment,
    measure_matched_flux,
    plan_injection_grid,
)
from astrostack.stack import combine, optimal_coadd


def _fluxes(frames):
    sigma = float(np.median([f.quality.noise_sigma for f in frames]))
    return np.array([8, 20, 60, 200], dtype=float) * sigma


@pytest.mark.parametrize(
    "combiner",
    [
        pytest.param(optimal_coadd, id="optimal"),
        pytest.param(lambda fs: combine(fs, method="sigma-clip"), id="sigma-clip"),
    ],
)
def test_recovery_is_linear_in_flux(homogeneous_corpus, combiner):
    _field, frames, _ = homogeneous_corpus
    report = injection_experiment(
        frames, combiner, fluxes=_fluxes(frames), n_sources=36, seed=4242
    )

    assert report.slope == pytest.approx(1.0, abs=0.15), (
        f"recovery slope {report.slope:.4f}: the pipeline "
        f"{'loses' if report.slope < 1 else 'ADDS'} flux"
    )
    assert report.r_squared > 0.97, f"recovery is not linear (R2={report.r_squared:.4f})"
    assert abs(report.intercept) < 0.25 * float(np.median(report.injected_flux))
    assert report.is_linear
    assert not report.notes, report.notes


def test_recovery_curve_covers_the_full_flux_range(homogeneous_corpus):
    """The audit must span a real dynamic range, not one flux level."""
    _field, frames, _ = homogeneous_corpus
    report = injection_experiment(frames, optimal_coadd, fluxes=_fluxes(frames), n_sources=36)
    levels = np.unique(report.injected_flux)
    assert levels.size >= 4
    assert levels.max() / levels.min() > 10.0

    payload = report.as_dict()
    assert len(payload["curve"]) == report.n_sources
    assert set(payload["curve"][0]) == {"injected", "recovered", "snr", "detected"}


def test_bright_sources_are_detected_and_faint_ones_are_not(homogeneous_corpus):
    """Completeness must be a curve, not a step at zero."""
    _field, frames, _ = homogeneous_corpus
    sigma = float(np.median([f.quality.noise_sigma for f in frames]))
    report = injection_experiment(
        frames, optimal_coadd, fluxes=np.array([1.0, 4.0, 40.0, 400.0]) * sigma, n_sources=40
    )
    faint = report.detected[report.injected_flux == report.injected_flux.min()]
    bright = report.detected[report.injected_flux == report.injected_flux.max()]
    assert bright.all(), "the brightest injected sources must all be recovered"
    assert not faint.all(), "sources below the noise must not all be 'detected'"
    assert report.completeness_50 is not None


def test_a_pipeline_that_adds_flux_is_caught():
    """The audit must be able to fail. A combiner that inflates is rejected."""
    from tests.synthetic import make_corpus, subtract_known_sky

    field = make_corpus(
        n_frames=6, shape=(128, 128), n_stars=12, seed=808,
        fwhm_pixels=3.0, sky_level=200.0, dither_pixels=0.0,
    )  # fmt: skip
    frames = subtract_known_sky(field)

    def inflating(fs):
        result = optimal_coadd(fs)
        result.image = (result.image * 1.6).astype(np.float32)
        return result

    report = injection_experiment(
        frames, inflating, fluxes=_fluxes(frames), n_sources=25, seed=99
    )
    assert not report.is_linear
    assert report.slope > 1.3
    assert any("ADDS flux" in n for n in report.notes)


def test_a_pipeline_that_loses_flux_is_caught(homogeneous_corpus):
    _field, frames, _ = homogeneous_corpus

    def lossy(fs):
        result = optimal_coadd(fs)
        result.image = (result.image * 0.55).astype(np.float32)
        return result

    report = injection_experiment(frames, lossy, fluxes=_fluxes(frames), n_sources=25)
    assert not report.is_linear
    assert report.slope < 0.7
    assert any("loses flux" in n for n in report.notes)


def test_injection_grid_is_deterministic_and_spread_out():
    a = plan_injection_grid((128, 128), 25, np.array([100.0, 200.0]), seed=7)
    b = plan_injection_grid((128, 128), 25, np.array([100.0, 200.0]), seed=7)
    c = plan_injection_grid((128, 128), 25, np.array([100.0, 200.0]), seed=8)

    assert [(s.x, s.y, s.flux) for s in a] == [(s.x, s.y, s.flux) for s in b]
    assert [(s.x, s.y) for s in a] != [(s.x, s.y) for s in c]

    pts = np.array([[s.y, s.x] for s in a])
    d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(d2, np.inf)
    assert np.sqrt(d2.min()) > 8.0, "injected sources must not pile up on each other"

    # Sub-pixel phases must be varied, or a drizzle pipeline would be flattered.
    phases = pts % 1.0
    assert phases.std() > 0.15


def test_injection_uses_each_frames_own_psf(heterogeneous_corpus):
    """A blurry frame must receive a blurry star."""
    _, frames, _ = heterogeneous_corpus
    sources = plan_injection_grid((160, 160), 4, np.array([50000.0]), seed=11)
    injected = inject_into_frames(frames, sources, add_shot_noise=False)

    peaks = []
    for original, new in zip(frames, injected, strict=True):
        delta = new.data.astype(float) - original.data.astype(float)
        peaks.append(float(delta.max()))
        assert float(delta.sum()) == pytest.approx(4 * 50000.0, rel=0.05)

    sharp = np.mean(peaks[:4])
    blurry = np.mean(peaks[4:])
    assert sharp > 2.0 * blurry, (sharp, blurry)


def test_matched_flux_estimator_is_unbiased(homogeneous_corpus):
    """The measurement tool must itself be unbiased before it can audit."""
    field, frames, _ = homogeneous_corpus
    result = optimal_coadd(frames)
    positions = field.truth.brightest(10)
    truth = np.sort(field.truth.flux)[::-1][:10]
    measured = measure_matched_flux(result.image, result.psf, positions, 1.0)
    assert float(np.median(measured / truth)) == pytest.approx(1.0, rel=0.08)
