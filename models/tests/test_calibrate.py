"""Calibration: masters, cosmic rays, spatially varying sky."""

from __future__ import annotations

import numpy as np
import pytest

from astrostack.calibrate import (
    apply_calibration,
    combine_masters,
    estimate_background,
    lacosmic,
    sigma_clipped_median,
    subtract_background,
)
from astrostack.io.frame import Frame, FrameMetadata, FrameQuality
from astrostack.rng import generator
from tests.synthetic import make_corpus


# --------------------------------------------------------------------------
# Masters
# --------------------------------------------------------------------------
def test_sigma_clipped_median_ignores_a_single_wild_frame():
    rng = generator(1, "clip")
    stack = rng.normal(100.0, 2.0, (9, 16, 16)).astype(np.float32)
    stack[4, 8, 8] = 100000.0
    med, used = sigma_clipped_median(stack)
    assert med[8, 8] == pytest.approx(100.0, abs=2.0)
    assert used[8, 8] < 9


def test_bias_dark_flat_are_applied_and_declared():
    rng = generator(2, "cal")
    shape = (64, 64)
    bias = np.full(shape, 300.0, dtype=np.float32)
    dark = np.full(shape, 20.0, dtype=np.float32)
    gy, gx = np.mgrid[0 : shape[0], 0 : shape[1]]
    vignette = 1.0 - 0.4 * (((gx - 32) / 32) ** 2 + ((gy - 32) / 32) ** 2) / 2

    masters = combine_masters(
        bias_stack=np.stack([bias + rng.normal(0, 1, shape) for _ in range(5)]),
        dark_stack=np.stack([bias + dark + rng.normal(0, 1, shape) for _ in range(5)]),
        flat_stack=np.stack([bias + 10000.0 * vignette for _ in range(5)]),
        dark_exposure_s=60.0,
    )
    assert masters.bias is not None and masters.dark is not None and masters.flat is not None
    assert float(np.median(masters.bias)) == pytest.approx(300.0, abs=1.0)
    assert float(np.median(masters.dark)) == pytest.approx(20.0, abs=1.0)
    assert float(np.median(masters.flat)) == pytest.approx(1.0, abs=0.02)

    signal = 1000.0
    raw = bias + dark + signal * vignette
    frame = Frame(
        frame_id="f",
        data=raw.astype(np.float32),
        meta=FrameMetadata(photo_id="f", exposure_seconds=60.0),
        quality=FrameQuality(noise_sigma=5.0),
    )
    out = apply_calibration(frame, masters)

    # Vignetting removed: the corners now match the centre. The absolute
    # scale is signal * median(flat) by construction — a flat is normalised to
    # its own median, so it fixes the *shape* of the response and leaves the
    # overall throughput to the photometric zero point.
    inner = float(np.median(out.data[24:40, 24:40]))
    corner = float(np.median(out.data[4:12, 4:12]))
    assert inner == pytest.approx(corner, rel=0.08), (inner, corner)
    assert masters.flat_normalisation is not None
    expected = signal * float(np.median(vignette))
    assert inner == pytest.approx(expected, rel=0.06)
    assert any("bias" in h and "flux-preserving" in h for h in out.history)


def test_flat_floor_masks_rather_than_amplifies():
    """Dividing by a tiny flat amplifies noise without adding signal."""
    shape = (32, 32)
    flat = np.ones(shape, dtype=np.float32)
    flat[:4, :4] = 0.02  # a dead corner
    masters = combine_masters(flat_stack=np.stack([flat * 1000.0] * 3))

    frame = Frame(
        frame_id="f", data=np.full(shape, 500.0, dtype=np.float32),
        meta=FrameMetadata(photo_id="f"), quality=FrameQuality(noise_sigma=5.0),
    )  # fmt: skip
    out = apply_calibration(frame, masters, flat_floor=0.2)
    assert out.mask is not None
    assert out.mask[:4, :4].all()
    assert not out.mask[16:, 16:].any()


def test_calibration_without_masters_is_a_no_op():
    frame = Frame(
        frame_id="f", data=np.ones((16, 16), dtype=np.float32), meta=FrameMetadata(photo_id="f")
    )
    out = apply_calibration(frame, combine_masters())
    assert np.array_equal(out.data, frame.data)
    assert any("no calibration frames" in h for h in out.history)


# --------------------------------------------------------------------------
# Cosmic rays
# --------------------------------------------------------------------------
def test_lacosmic_finds_cosmic_rays_and_spares_stars():
    """van Dokkum (2001): a cosmic ray is sharper than the PSF, a star is not."""
    field = make_corpus(
        n_frames=1, shape=(128, 128), n_stars=25, seed=77,
        fwhm_pixels=3.5, sky_level=300.0, dither_pixels=0.0,
    )  # fmt: skip
    frame = field.frames[0]

    data = frame.data.astype(np.float64).copy()
    rng = generator(5, "cr")
    positions = [(int(rng.integers(10, 118)), int(rng.integers(10, 118))) for _ in range(12)]
    for y, x in positions:
        data[y, x] += 4000.0

    result = lacosmic(data, gain_e_per_adu=1.0, read_noise_e=5.0, sigclip=4.5, objlim=5.0)
    found = sum(bool(result.mask[y, x]) for y, x in positions)
    assert found >= 9, f"only {found}/12 cosmic rays flagged"

    # Star cores must survive: they are exactly PSF-shaped.
    star_pixels = 0
    for x, y in zip(field.truth.x, field.truth.y, strict=True):
        iy, ix = round(y), round(x)
        if 2 <= iy < 126 and 2 <= ix < 126:
            star_pixels += int(result.mask[iy, ix])
    assert star_pixels == 0, f"{star_pixels} star cores were flagged as cosmic rays"
    assert result.fraction() < 0.01


def test_lacosmic_is_quiet_on_a_clean_frame():
    field = make_corpus(
        n_frames=1, shape=(96, 96), n_stars=0, seed=78, sky_level=400.0, empty=True,
        dither_pixels=0.0,
    )  # fmt: skip
    result = lacosmic(field.frames[0].data, gain_e_per_adu=1.0, read_noise_e=5.0)
    assert result.fraction() < 0.002, result.fraction()


def test_lacosmic_can_clean_when_asked():
    data = np.full((64, 64), 100.0)
    data[32, 32] = 9000.0
    result = lacosmic(data, sigclip=4.0, clean=True)
    assert result.cleaned is not None
    assert result.cleaned[32, 32] < 500.0


# --------------------------------------------------------------------------
# Background
# --------------------------------------------------------------------------
def test_background_removes_a_light_pollution_gradient():
    field = make_corpus(
        n_frames=1, shape=(192, 192), n_stars=20, seed=88,
        fwhm_pixels=3.0, sky_level=500.0, sky_gradient=0.8, dither_pixels=0.0,
    )  # fmt: skip
    frame = field.frames[0]
    before = float(np.ptp(np.median(frame.data, axis=0)))

    out = subtract_background(frame, box_size=24, filter_size=3)
    full = float(np.ptp(np.median(out.data, axis=0)))
    # The mesh median filter (SExtractor's defence against object-contaminated
    # cells) unavoidably flattens a steep ramp within half a box of the frame
    # edge, so the interior is measured separately. See the note in
    # astrostack.calibrate.background.
    interior = float(np.ptp(np.median(out.data[24:-24, 24:-24], axis=0)))

    assert interior < 0.10 * before, (before, interior)
    assert full < 0.30 * before, (before, full)
    assert out.quality.background_adu == pytest.approx(500.0, rel=0.6)
    assert out.quality.sky_gradient_amplitude > 100.0
    assert out.quality.noise_sigma is not None and out.quality.noise_sigma > 0
    assert abs(float(np.median(out.data))) < 0.1 * 500.0


def test_background_preserves_source_flux():
    """Subtracting the sky must not eat the stars it sits under."""
    from astrostack.metrics.injection import measure_matched_flux

    field = make_corpus(
        n_frames=1, shape=(192, 192), n_stars=12, seed=89,
        fwhm_pixels=3.0, sky_level=400.0, sky_gradient=0.5, dither_pixels=0.0,
        flux_range=(3000.0, 30000.0),
    )  # fmt: skip
    frame = field.frames[0]
    out = subtract_background(frame, box_size=48)

    psf = frame.psf.normalised()
    positions = field.truth.positions
    truth = field.truth.flux
    measured = measure_matched_flux(out.data, psf, positions, 1.0)
    assert float(np.median(measured / truth)) == pytest.approx(1.0, rel=0.15)


def test_background_falls_back_on_a_tiny_frame():
    """A 16x16 test image must not crash Background2D's mesh logic."""
    model = estimate_background(np.full((16, 16), 7.0, dtype=np.float32))
    assert model.background.shape == (16, 16)
    assert model.median_level == pytest.approx(7.0)
    assert model.rms_median > 0
