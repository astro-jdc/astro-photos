"""Noise in, nothing out.

The complement of the injection test. A pipeline that manufactures sources
from noise is worse than useless in astronomy: a hallucinated point source is
a false discovery, not an aesthetic defect (hard rule 2 of ``CLAUDE.md``).

The test threshold is not arbitrary. A 5-sigma matched-filter threshold on
Gaussian noise gives a one-sided false-alarm probability of 2.87e-7 per
independent resolution element, so on a 160x160 image with ~10 pixels per beam
the *expected* number of spurious peaks is far below one. Finding several
means the pipeline is generating structure.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from astrostack.align.stars import detect_sources, gaussian_kernel
from astrostack.metrics.injection import false_positive_rate
from astrostack.robust import robust_sigma
from astrostack.stack import combine, drizzle, optimal_coadd
from tests.synthetic import make_corpus, subtract_known_sky


@pytest.fixture(scope="module")
def empty_corpus():
    field = make_corpus(
        n_frames=8, shape=(192, 192), n_stars=0, seed=1234,
        fwhm_pixels=3.0, sky_level=250.0, dither_pixels=0.0, empty=True,
    )  # fmt: skip
    return field, subtract_known_sky(field)


@pytest.mark.parametrize(
    "combiner",
    [
        pytest.param(optimal_coadd, id="optimal"),
        pytest.param(lambda fs: combine(fs, method="sigma-clip"), id="sigma-clip"),
        pytest.param(lambda fs: combine(fs, method="mean"), id="mean"),
        pytest.param(lambda fs: combine(fs, method="median"), id="median"),
    ],
)
def test_no_sources_are_manufactured_from_noise(empty_corpus, combiner):
    _, frames = empty_corpus
    result = combiner(frames)
    psf = result.psf if result.psf is not None else gaussian_kernel(3.0, size=21)

    # These coadds are not resampled, so the white-noise convention is valid
    # and the declared per-pixel sigma is the honest threshold to use.
    sigma = robust_sigma(result.image, mask=(result.weight <= 0))
    stats = false_positive_rate(result.image, psf, sigma, threshold=5.0)

    beams = stats["n_independent_beams"]
    expected = beams * (1.0 - norm.cdf(5.0))
    allowed = max(3.0, 5.0 * expected)
    assert stats["n_detections"] <= allowed, (
        f"{stats['n_detections']} 5-sigma peaks in pure noise "
        f"({expected:.4f} expected by chance over {beams:.0f} beams)"
    )


def test_source_detector_finds_nothing_in_an_empty_coadd(empty_corpus):
    """The whole detection path, not just the matched filter, must stay quiet."""
    _, frames = empty_corpus
    result = optimal_coadd(frames)
    catalog = detect_sources(result.image, threshold_sigma=6.0, min_area=5)
    assert len(catalog) <= 2, f"{len(catalog)} spurious detections at 6 sigma"


def test_drizzle_does_not_manufacture_sources(empty_corpus):
    """Resampling to a finer grid must not conjure structure either.

    This is the failure mode that "super-resolution" claims usually hide:
    upsampling always *looks* like it added detail.
    """
    from astrostack.align import make_output_grid

    _, frames = empty_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=1.0)
    result = drizzle(frames, grid, pixfrac=0.8, subsample=5)

    psf = result.psf if result.psf is not None else gaussian_kernel(3.0, size=21)
    # Drizzled noise is CORRELATED between neighbouring output pixels, so the
    # threshold has to be calibrated on the filtered detection image rather
    # than assumed from a per-pixel sigma (noise=None does that).
    stats = false_positive_rate(result.image, psf, None, threshold=5.0)
    beams = stats["n_independent_beams"]
    allowed = max(3.0, 5.0 * beams * (1.0 - norm.cdf(5.0)))
    assert stats["n_detections"] <= allowed, stats


def test_deconvolution_does_not_manufacture_sources(empty_corpus):
    """Bounded Richardson-Lucy on noise must stay a noise field.

    Deconvolution is where hallucination is easiest: run it long enough and RL
    converges towards the noise realisation, breaking a smooth background into
    convincing little knots.
    """
    from astrostack.enhance.deconv import operator_psf, richardson_lucy

    _, frames = empty_corpus
    result = optimal_coadd(frames)
    deconvolved = richardson_lucy(result.image, result.psf, iterations=12)

    # After deconvolution the image no longer has the PSF it started with, and
    # its noise is neither white nor at the old level. Both the filter and the
    # threshold therefore come from the deconvolved product itself.
    post_psf = operator_psf(result.psf, "richardson-lucy", iterations=12)
    stats = false_positive_rate(deconvolved.image, post_psf, None, threshold=6.0)
    beams = stats["n_independent_beams"]
    allowed = max(3.0, 5.0 * beams * (1.0 - norm.cdf(6.0)))
    assert stats["n_detections"] <= allowed, stats
    assert deconvolved.warnings == [] or all(isinstance(w, str) for w in deconvolved.warnings)


def test_uncertainty_map_is_published_and_matches_the_noise(empty_corpus):
    """An honest uncertainty map is what lets a user judge a marginal source."""
    _, frames = empty_corpus
    result = optimal_coadd(frames)
    assert result.uncertainty is not None
    covered = result.weight > 0
    declared = float(np.median(result.uncertainty[covered]))
    measured = float(np.std(result.image[covered]))
    assert declared > 0
    assert measured == pytest.approx(declared, rel=0.15), (declared, measured)


def test_false_positive_helper_does_detect_a_real_source(empty_corpus):
    """The control must be able to see something, or it proves nothing."""
    _, frames = empty_corpus
    result = optimal_coadd(frames)
    sigma = robust_sigma(result.image, mask=(result.weight <= 0))

    planted = result.image.astype(np.float64).copy()
    k = np.asarray(result.psf, dtype=np.float64)
    ky, kx = k.shape
    cy, cx = planted.shape[0] // 2, planted.shape[1] // 2
    planted[cy - ky // 2 : cy + ky // 2 + 1, cx - kx // 2 : cx + kx // 2 + 1] += k * 60.0 * sigma

    stats = false_positive_rate(planted, result.psf, sigma, threshold=5.0)
    assert stats["n_detections"] >= 1
    assert false_positive_rate(planted, result.psf, None, threshold=5.0)["n_detections"] >= 1
