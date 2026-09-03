"""SNR must grow as sqrt(N).

This is *the* physical property the whole product rests on. Section 5's table
puts it first: depth scales as ``sqrt(sum t_i * throughput_i)`` in the
background-limited regime, and hundreds of contributions go several magnitudes
deeper than any single frame.

If this test fails, nothing else in the package is worth reading.

Method note: the noise is measured on an **identically processed source-free
stack**, not inside the science image. Measuring it in-image would be
confusion-limited — the faint tail of a realistic luminosity function puts
real flux nearly everywhere, and that flux does not average down with N — so
the measurement would show sqrt(N) saturating when the noise is in fact still
falling.
"""

from __future__ import annotations

import numpy as np
import pytest

from astrostack.stack import combine, optimal_coadd
from tests.conftest import empirical_snr
from tests.synthetic import make_corpus, subtract_known_sky

COUNTS = (1, 2, 4, 8)


@pytest.mark.parametrize("method", ["mean", "sigma-clip"])
def test_snr_grows_as_sqrt_n(homogeneous_corpus, method):
    """Median detection SNR of the same stars against N, on a common grid."""
    field, frames, empty = homogeneous_corpus
    positions = field.truth.brightest(12)

    snrs = []
    for n in COUNTS:
        result = combine(frames[:n], method=method, weighting="inverse-variance")
        noise_ref = combine(empty[:n], method=method, weighting="inverse-variance")
        snrs.append(empirical_snr(result, positions, noise_result=noise_ref))

    ratios = np.array(snrs) / snrs[0]
    expected = np.sqrt(np.array(COUNTS, dtype=float))
    assert np.allclose(ratios, expected, rtol=0.12), (
        f"{method}: SNR ratios {ratios.round(3).tolist()} vs sqrt(N) {expected.round(3).tolist()}"
    )


def test_optimal_coadd_also_scales_as_sqrt_n(homogeneous_corpus):
    """The Zackay-Ofek statistic obeys the same physics, not a different one."""
    field, frames, empty = homogeneous_corpus
    positions = field.truth.brightest(12)

    snrs = [
        empirical_snr(optimal_coadd(frames[:n]), positions, noise_result=optimal_coadd(empty[:n]))
        for n in COUNTS
    ]
    ratios = np.array(snrs) / snrs[0]
    assert np.allclose(ratios, np.sqrt(COUNTS), rtol=0.12), ratios.round(3).tolist()


def test_declared_f_r_matches_sqrt_n():
    """F_R of eq. (4) must equal sqrt(N) * F / sigma for identical frames.

    A closed-form check on the coaddition mathematics that depends on no noise
    realisation at all.
    """
    field = make_corpus(
        n_frames=9, shape=(96, 96), n_stars=10, seed=55,
        fwhm_pixels=3.0, sky_level=400.0, dither_pixels=0.0,
    )  # fmt: skip
    frames = subtract_known_sky(field)
    sigma = float(frames[0].quality.noise_sigma)
    result = optimal_coadd(frames)
    assert result.metrics["f_r"] == pytest.approx(np.sqrt(len(frames)) / sigma, rel=1e-6)


def test_declared_uncertainty_matches_measured_noise(homogeneous_corpus):
    """The published uncertainty map must not be fiction.

    The pixel scatter of a source-free coadd has to agree with the 1/F_R the
    optimal coadd advertises, otherwise every SNR the product reports is wrong
    by a constant factor.
    """
    _, _, empty = homogeneous_corpus
    result = optimal_coadd(empty)
    declared = float(np.median(result.uncertainty[result.weight > 0]))
    measured = float(np.std(result.image[result.weight > 0]))
    assert measured == pytest.approx(declared, rel=0.12), (declared, measured)


def test_deeper_stack_detects_fainter_sources():
    """More frames must push the detection limit down, not just look smoother.

    The corpus here is deliberately faint: most sources sit below the 2-frame
    limit, so the count at a fixed 5-sigma threshold has room to grow.
    """
    field = make_corpus(
        n_frames=12, shape=(192, 192), n_stars=45, seed=606,
        fwhm_pixels=3.0, sky_level=400.0, dither_pixels=0.0,
        flux_range=(120.0, 2500.0),
    )  # fmt: skip
    frames = subtract_known_sky(field)
    positions = field.truth.positions

    from astrostack.metrics.quality import matched_filter_snr

    def detections(result):
        noise = result.uncertainty if result.uncertainty is not None else 1.0
        snr = matched_filter_snr(result.image, result.psf, positions, noise)
        return int(np.nansum(snr >= 5.0))

    shallow = detections(optimal_coadd(frames[:2]))
    deep = detections(optimal_coadd(frames))
    assert deep > shallow, (shallow, deep)
    assert shallow < len(field.truth.flux), "the test corpus is not faint enough to be informative"
