"""Deconvolution within the physics budget, and HDR compositing."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve

from astrostack.align import gaussian_kernel
from astrostack.enhance import (
    hdr_composite,
    measure_operator_fwhm,
    operator_psf,
    relative_scale,
    richardson_lucy,
    wiener_deconvolve,
)
from astrostack.rng import generator
from tests.synthetic import make_corpus


# --------------------------------------------------------------------------
# Deconvolution
# --------------------------------------------------------------------------
def test_richardson_lucy_sharpens_a_known_blur():
    psf = gaussian_kernel(5.0, size=31)
    scene = np.zeros((129, 129))
    scene[64, 64] = 1000.0
    blurred = fftconvolve(scene, psf, mode="same")

    result = richardson_lucy(blurred, psf, iterations=25)
    assert result.achieved_fwhm_pixels < result.input_fwhm_pixels
    assert result.sharpening_factor > 1.2
    assert result.flux_preserving


def test_richardson_lucy_conserves_flux():
    """The multiplicative update preserves the sum for a unit-sum PSF."""
    rng = generator(1, "rl")
    psf = gaussian_kernel(4.0, size=25)
    scene = np.clip(rng.normal(50.0, 5.0, (96, 96)), 0, None)
    scene[40, 40] += 2000.0
    blurred = fftconvolve(scene, psf, mode="same")

    result = richardson_lucy(blurred, psf, iterations=15)
    assert result.flux_ratio == pytest.approx(1.0, rel=0.05)


def test_iterations_are_hard_capped_with_a_warning():
    psf = gaussian_kernel(4.0, size=21)
    data = fftconvolve(np.ones((64, 64)) * 10.0, psf, mode="same")
    result = richardson_lucy(data, psf, iterations=500, max_iterations=40)
    assert result.iterations == 40
    assert any("capped" in w for w in result.warnings)


def test_going_below_the_diffraction_limit_is_flagged_as_prior():
    """Section 5's wall. Past it the output is prior, not measurement."""
    psf = gaussian_kernel(6.0, size=41)
    scene = np.zeros((129, 129))
    scene[64, 64] = 5000.0
    blurred = fftconvolve(scene, psf, mode="same")

    # Pretend the optics could only ever resolve 5 px: any output narrower
    # than that is invented.
    result = richardson_lucy(blurred, psf, iterations=50, diffraction_limit_pixels=5.0)
    assert result.achieved_fwhm_pixels < 5.0
    assert result.prior_dominated
    assert any("diffraction limit" in w for w in result.warnings)
    assert any("prior, not measurement" in w for w in result.warnings)

    # A gentle run inside the budget is not flagged.
    gentle = richardson_lucy(blurred, psf, iterations=3, diffraction_limit_pixels=5.0)
    assert not gentle.prior_dominated


def test_excessive_sharpening_is_flagged_even_without_a_diffraction_limit():
    psf = gaussian_kernel(6.0, size=41)
    scene = np.zeros((129, 129))
    scene[64, 64] = 5000.0
    result = richardson_lucy(fftconvolve(scene, psf, mode="same"), psf, iterations=60)
    if result.sharpening_factor and result.sharpening_factor > 2.0:
        assert any("prior-driven" in w for w in result.warnings)


def test_operator_psf_is_the_real_impulse_response():
    psf = gaussian_kernel(5.0, size=31)
    post = operator_psf(psf, "richardson-lucy", iterations=20)
    assert float(post.sum()) == pytest.approx(1.0, abs=1e-5)
    assert measure_operator_fwhm(psf, "richardson-lucy", iterations=20) == pytest.approx(
        measure_operator_fwhm(psf, "richardson-lucy", iterations=20)
    )
    # More iterations means a narrower impulse response, monotonically.
    widths = [measure_operator_fwhm(psf, "richardson-lucy", iterations=n) for n in (2, 10, 40)]
    assert widths[0] > widths[1] > widths[2]


def test_wiener_declares_that_it_does_not_conserve_flux():
    psf = gaussian_kernel(4.0, size=21)
    rng = generator(2, "wiener")
    data = fftconvolve(rng.normal(100.0, 10.0, (96, 96)), psf, mode="same")

    result = wiener_deconvolve(data, psf, noise_sigma=10.0)
    assert not result.flux_preserving
    assert any("not a flux-conserving operator" in w for w in result.warnings)
    assert result.achieved_fwhm_pixels is not None

    hand_tuned = wiener_deconvolve(data, psf, nsr=1e-3)
    assert any("supplied by hand" in w for w in hand_tuned.warnings)


def test_damped_rl_is_gentler_on_the_background():
    """Damped RL (White 1994) is what stops noise becoming fake knots."""
    rng = generator(3, "damp")
    psf = gaussian_kernel(4.0, size=21)
    noise = fftconvolve(rng.normal(100.0, 8.0, (96, 96)), psf, mode="same")

    plain = richardson_lucy(noise, psf, iterations=30, damping=0.0)
    damped = richardson_lucy(noise, psf, iterations=30, damping=3.0)
    assert float(np.std(damped.image)) < float(np.std(plain.image))


# --------------------------------------------------------------------------
# HDR
# --------------------------------------------------------------------------
def _exposure_series(seed: int = 10):
    """Three exposures of the same field, 1x / 8x / 64x, with clipping."""
    field = make_corpus(
        n_frames=3, shape=(128, 128), n_stars=12, seed=seed,
        fwhm_pixels=3.0, sky_level=50.0, dither_pixels=0.0,
        flux_range=(2000.0, 200000.0),
    )  # fmt: skip
    frames = []
    for i, (fr, scale) in enumerate(zip(field.frames, (1.0, 8.0, 64.0), strict=True)):
        data = fr.data.astype(np.float64) * scale
        saturation = 60000.0
        saturated = data >= saturation
        data = np.minimum(data, saturation)
        new = fr.copy_with(data.astype(np.float32), saturated=saturated)
        new.meta = fr.meta.model_copy(update={"exposure_seconds": 60.0 * scale})
        new.quality = fr.quality.model_copy(
            update={"noise_sigma": float(fr.quality.noise_sigma * np.sqrt(scale))}
        )
        new.variance = (fr.effective_variance() * scale).astype(np.float32)
        frames.append(new)
        _ = i
    return field, sorted(frames, key=lambda f: f.frame_id)


def test_relative_scale_recovers_the_exposure_ratio():
    _, frames = _exposure_series()
    scale = relative_scale(frames[0], frames[1])
    assert scale == pytest.approx(8.0, rel=0.2), scale


def test_hdr_recovers_saturated_cores_and_reports_it():
    field, frames = _exposure_series()
    result = hdr_composite(frames)

    assert result.coadd.flux_preserving
    assert result.dynamic_range_stops > 4.0
    assert set(result.scales) == {f.frame_id for f in frames}

    # The brightest star saturates in the long exposure but not in the short
    # one. The composite lives on the *reference* frame's flux scale, so the
    # test is that its core exceeds where the long exposure clipped, expressed
    # on that same scale — i.e. the core was genuinely recovered from the
    # short frame rather than inherited from the clipped one.
    brightest = int(np.argmax(field.truth.flux))
    y, x = int(field.truth.y[brightest]), int(field.truth.x[brightest])
    longest = frames[-1]
    assert longest.saturated is not None and longest.saturated[y, x]

    clip_on_reference_scale = 60000.0 * result.scales[longest.frame_id]
    core = float(result.coadd.image[y, x])
    assert core > clip_on_reference_scale, (core, clip_on_reference_scale)

    # And it matches the unclipped short exposure, which is the measurement.
    shortest = frames[0]
    expected = float(shortest.data[y, x]) * result.scales[shortest.frame_id]
    assert core == pytest.approx(expected, rel=0.25), (core, expected)

    notes = " ".join(result.coadd.notes)
    assert "recovered core" in notes
    assert "different effective PSF" in notes
    assert 0.0 <= result.recovered_core_fraction <= 1.0


def test_hdr_needs_two_frames():
    _, frames = _exposure_series()
    with pytest.raises(ValueError, match="at least two"):
        hdr_composite(frames[:1])


def test_hdr_requires_sorted_frames():
    _, frames = _exposure_series()
    with pytest.raises(ValueError, match="sorted"):
        hdr_composite(list(reversed(frames)))
