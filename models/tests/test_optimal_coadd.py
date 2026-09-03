"""Zackay & Ofek coaddition must beat a sigma-clipped mean on unequal data.

The claim being tested is the one from section 3 of the research note: for
images of unequal quality the optimal combination is *not* a weighted average,
because a scalar weight cannot express the fact that each frame has its own
PSF. Given a corpus with seeing varying by several times, matched-filtering
each frame with its own PSF before summing is worth a large fraction of a
magnitude.

The baseline is deliberately a good one (rule 7: no straw men): a
sigma-clipped mean with **inverse-variance weighting** and its true effective
PSF, matched-filtered exactly like the optimal coadd, with the noise of both
measured on identically processed source-free stacks.
"""

from __future__ import annotations

import numpy as np
import pytest

from astrostack.stack import combine, optimal_coadd
from tests.conftest import empirical_snr
from tests.synthetic import make_corpus, subtract_known_sky


def _pair(frames, empty, method="sigma-clip", weighting="inverse-variance"):
    optimal = optimal_coadd(frames)
    optimal_noise = optimal_coadd(empty)
    baseline = combine(frames, method=method, weighting=weighting)
    baseline_noise = combine(empty, method=method, weighting=weighting)
    return (optimal, optimal_noise), (baseline, baseline_noise)


def test_optimal_beats_sigma_clip_on_unequal_quality(heterogeneous_corpus):
    """Four sharp/bright-sky frames plus four blurry/dark-sky frames.

    Inverse-variance weighting prefers the dark-sky frames, which are exactly
    the blurry ones — so the scalar-weighted mean loses. This anti-correlation
    is not contrived: good seeing and dark sky are independent conditions and
    a public corpus contains every combination of them.
    """
    field, frames, empty = heterogeneous_corpus
    positions = field.truth.brightest(12)

    (optimal, opt_noise), (baseline, base_noise) = _pair(frames, empty)
    snr_optimal = empirical_snr(optimal, positions, noise_result=opt_noise)
    snr_baseline = empirical_snr(baseline, positions, noise_result=base_noise)

    gain = snr_optimal / snr_baseline
    assert gain > 1.10, (
        f"optimal coaddition gained only {gain:.3f}x over the sigma-clipped mean "
        f"({snr_optimal:.1f} vs {snr_baseline:.1f}); on unequal-quality input it should "
        "win clearly, or the per-image matched filter is not doing its job"
    )
    # Sanity: state the gain in magnitudes, which is how the claim is made.
    assert 2.5 * np.log10(gain) > 0.1


def test_optimal_beats_sigma_clip_on_seeing_spread_alone():
    """Same noise in every frame, seeing varying 3x.

    Here a scalar weight has *nothing* to work with — every frame has the same
    variance — so any gain is entirely due to the per-image PSF matched
    filter. This isolates the mechanism.
    """
    kwargs = dict(
        n_frames=8, shape=(160, 160), n_stars=25, seed=707,
        fwhm_pixels=[2.0] * 4 + [6.0] * 4, sky_level=[300.0] * 8, dither_pixels=0.0,
    )  # fmt: skip
    field = make_corpus(**kwargs)
    frames = subtract_known_sky(field)
    empty = subtract_known_sky(make_corpus(empty=True, **kwargs))
    positions = field.truth.brightest(12)

    (optimal, opt_noise), (baseline, base_noise) = _pair(frames, empty)
    gain = empirical_snr(optimal, positions, noise_result=opt_noise) / empirical_snr(
        baseline, positions, noise_result=base_noise
    )
    assert gain > 1.08, f"per-PSF matched filter gained only {gain:.3f}x"


def test_optimal_matches_the_mean_when_frames_are_identical(homogeneous_corpus):
    """With identical PSFs and sigmas the two must agree.

    The theory says the optimal statistic *reduces* to the matched-filtered
    weighted mean when every frame is the same. A large gain here would mean
    the comparison is unfair, not that the algorithm is clever.
    """
    field, frames, empty = homogeneous_corpus
    positions = field.truth.brightest(12)

    optimal = optimal_coadd(frames, epsilon=1e-7)
    optimal_noise = optimal_coadd(empty, epsilon=1e-7)
    baseline = combine(frames, method="mean")
    baseline_noise = combine(empty, method="mean")
    ratio = empirical_snr(optimal, positions, noise_result=optimal_noise) / empirical_snr(
        baseline, positions, noise_result=baseline_noise
    )
    assert ratio == pytest.approx(1.0, rel=0.06), f"ratio {ratio:.4f} on identical frames"


def test_effective_psf_is_narrower_than_the_worst_input(heterogeneous_corpus):
    """Paper II: the proper coadd has a well-defined, narrower effective PSF.

    Measured with the noise-equivalent width, not a second moment: the second
    moment of a Moffat diverges, so a moment-based FWHM would mostly measure
    where the kernel was truncated.
    """
    from astrostack.metrics.quality import noise_equivalent_fwhm

    _field, frames, _ = heterogeneous_corpus
    result = optimal_coadd(frames)
    assert result.psf is not None

    coadd_width = noise_equivalent_fwhm(result.psf)
    input_widths = [noise_equivalent_fwhm(f.psf.normalised()) for f in frames if f.psf]
    assert coadd_width < max(input_widths), (coadd_width, input_widths)
    # And it is a real, unit-sum kernel, not a normalisation artefact.
    assert float(result.psf.sum()) == pytest.approx(1.0, abs=1e-5)


def test_weights_follow_transparency_over_variance(heterogeneous_corpus):
    """The recorded per-frame weights must be F_j / sigma_j**2, normalised."""
    _, frames, _ = heterogeneous_corpus
    result = optimal_coadd(frames)

    expected = np.array(
        [float(f.quality.transparency or 1.0) / float(f.quality.noise_sigma) ** 2 for f in frames]
    )
    expected = expected / expected.sum()
    got = np.array([result.frame_weights[f.frame_id] for f in frames])
    assert np.allclose(got, expected, rtol=1e-6)
    assert result.frame_weights[frames[0].frame_id] != result.frame_weights[frames[-1].frame_id]


def test_photometrically_unreliable_frames_cannot_set_the_flux_scale(homogeneous_corpus):
    """A JPEG contributor adds depth but never drives F_j."""
    _, frames, _ = homogeneous_corpus
    doctored = []
    for i, f in enumerate(frames):
        g = f.copy_with(f.data)
        if i == 0:
            g.meta = f.meta.model_copy(
                update={"photometrically_unreliable": True, "unreliable_reason": "test"}
            )
            g.quality = f.quality.model_copy(update={"transparency": 7.0})
        doctored.append(g)

    result = optimal_coadd(sorted(doctored, key=lambda f: f.frame_id))
    transparencies = result.metrics["input_transparency"]
    assert transparencies[0] == 1.0, "an unreliable frame must not contribute F_j = 7"


def test_field_varying_psf_frames_are_reported(homogeneous_corpus):
    """A frame whose PSF varies across the field breaks the stationarity
    assumption, and the caller has to be told rather than silently misled."""
    from astrostack.io.frame import PSFModel

    _, frames, _ = homogeneous_corpus
    doctored = []
    for i, f in enumerate(frames):
        g = f.copy_with(f.data)
        if i == 0 and f.psf is not None:
            g.psf = PSFModel(
                kernel=f.psf.kernel,
                fwhm_pixels=f.psf.fwhm_pixels,
                field_grid_shape=(2, 2),
                field_fwhm=np.array([[2.0, 6.0], [2.5, 7.0]]),
            )
        doctored.append(g)

    result = optimal_coadd(sorted(doctored, key=lambda f: f.frame_id))
    assert frames[0].frame_id in result.metrics["field_varying_psf_frames"]


def test_requires_sorted_frames(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    with pytest.raises(ValueError, match="sorted"):
        optimal_coadd(list(reversed(frames)))


def test_requires_measured_psfs(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    stripped = [f.copy_with(f.data, psf=None) for f in frames]
    with pytest.raises(ValueError, match="no measured PSF"):
        optimal_coadd(stripped)
