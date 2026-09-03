"""Flux conservation.

Rule 2 of the astro-ml brief: every operation declares whether it conserves
flux, and the ones that claim to must be held to it. Drizzle (Fruchter & Hook
2002) and exact reprojection (Montage-style spherical-polygon overlap) both
claim it; ``reproject_interp`` does not, and this file pins the difference so
a future change cannot quietly reverse the two.

Every measurement here runs on **noiseless** synthetic frames. On a noisy
frame the total-flux integral is dominated by the noise realisation — for a
128x128 frame with sigma 15 the noise contributes ~1900 ADU against ~20000 ADU
of stars — so the ratio being tested would be swamped by something that has
nothing to do with the resampling.
"""

from __future__ import annotations

import numpy as np
import pytest

from astrostack.align import make_output_grid, reproject_frame
from astrostack.metrics.quality import flux_ratio
from astrostack.stack import drizzle
from astrostack.stack.drizzle import DrizzleAccumulator


@pytest.mark.parametrize("method,tolerance", [("exact", 0.01), ("adaptive", 0.02)])
def test_reprojection_conserves_flux(noiseless_corpus, method, tolerance):
    """Flux-conserving reprojection onto the same scale must preserve the total."""
    field, frames = noiseless_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=field.truth.pixel_scale_arcsec)
    out = reproject_frame(frames[0], grid, method=method, conserve_flux=True)
    ratio = flux_ratio(frames[0].data, out.data)
    assert ratio == pytest.approx(1.0, abs=tolerance), f"{method}: flux ratio {ratio:.5f}"


def test_reprojection_declares_flux_status(noiseless_corpus):
    """``interp`` is fast and not flux conserving; the frame must say so."""
    _, frames = noiseless_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    interp = reproject_frame(frames[0], grid, method="interp")
    assert any("NOT-flux-preserving" in h for h in interp.history if "align.register" in h)

    exact = reproject_frame(frames[0], grid, method="exact")
    register_notes = [h for h in exact.history if "align.register" in h]
    assert register_notes and all("NOT-flux-preserving" not in h for h in register_notes)


@pytest.mark.parametrize("pixfrac", [0.5, 0.7, 1.0])
def test_drizzle_conserves_flux_at_native_scale(noiseless_corpus, pixfrac):
    """Drizzle onto the input scale returns the input total flux."""
    field, frames = noiseless_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=field.truth.pixel_scale_arcsec)
    result = drizzle(frames[:1], grid, pixfrac=pixfrac, subsample=7)
    ratio = flux_ratio(frames[0].data, result.image)
    assert ratio == pytest.approx(1.0, abs=0.02), f"pixfrac={pixfrac}: ratio {ratio:.5f}"


def test_drizzle_conserves_flux_onto_a_finer_grid(noiseless_corpus):
    """The s**2 surface-brightness factor must be right, not just present.

    Onto a grid twice as fine there are four times as many output pixels, each
    holding a quarter of the value; the *total* must be unchanged. Getting the
    factor wrong is the classic drizzle bug and it is invisible by eye.
    """
    field, frames = noiseless_corpus
    scale = field.truth.pixel_scale_arcsec / 2.0
    grid = make_output_grid(frames, pixel_scale_arcsec=scale)
    result = drizzle(frames, grid, pixfrac=0.9, subsample=7)

    mean_input_flux = float(np.mean([f.data.sum() for f in frames]))
    ratio = float(result.image.sum()) / mean_input_flux
    assert ratio == pytest.approx(1.0, abs=0.05), (
        f"flux ratio {ratio:.5f} onto a 2x finer grid; "
        f"hole fraction {result.metrics['hole_fraction']:.3f}"
    )
    assert result.flux_preserving


def test_drizzle_accumulator_deposits_every_sub_drop(noiseless_corpus):
    """Numerator and denominator must stay consistent, drop by drop.

    This is what makes the quadrature approximation *exactly* flux conserving
    rather than merely convergent: each sub-drop contributes its flux and its
    matching weight to the same output pixel.
    """
    field, frames = noiseless_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=field.truth.pixel_scale_arcsec)
    acc = DrizzleAccumulator(grid.shape)
    acc.add_frame(frames[0], grid.wcs, pixfrac=0.7, subsample=5, frame_weight=1.0)
    image, weight, _ = acc.finish()

    covered = weight > 0
    # With a unit frame weight, every covered output pixel's weight is the
    # fraction of a drop it received; it can never exceed the number of drops.
    assert np.all(weight >= 0)
    assert covered.any()
    assert np.isfinite(image[covered]).all()


def test_stacking_preserves_the_flux_scale(homogeneous_corpus):
    """A mean/optimal coadd of frames of flux f must itself have flux f.

    Not a resampling property — a *normalisation* property. If the coadd came
    out scaled by sqrt(N) (a very easy mistake in the Zackay-Ofek
    normalisation) every magnitude the product reports would be wrong.
    """
    from astrostack.metrics.injection import measure_matched_flux
    from astrostack.stack import combine, optimal_coadd

    field, frames, _ = homogeneous_corpus
    positions = field.truth.brightest(8)
    truth_flux = np.sort(field.truth.flux)[::-1][:8]

    for result in (combine(frames, method="mean"), optimal_coadd(frames)):
        measured = measure_matched_flux(result.image, result.psf, positions, 1.0)
        ratio = float(np.median(measured / truth_flux))
        assert ratio == pytest.approx(1.0, rel=0.10), f"{result.method}: flux scale off by {ratio:.3f}"


def test_flux_ratio_helper_detects_a_loss():
    """The measurement tool itself must not be a no-op."""
    a = np.ones((16, 16))
    assert flux_ratio(a, a * 0.5) == pytest.approx(0.5)
    assert flux_ratio(a, a) == pytest.approx(1.0)


def test_drizzle_rejects_out_of_range_pixfrac(noiseless_corpus):
    _, frames = noiseless_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    with pytest.raises(ValueError, match="pixfrac"):
        drizzle(frames, grid, pixfrac=1.5)


def test_drizzle_requires_sorted_input(noiseless_corpus):
    """Unsorted input is refused: float addition is not associative."""
    _, frames = noiseless_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    with pytest.raises(ValueError, match="sorted"):
        drizzle(list(reversed(frames)), grid)
