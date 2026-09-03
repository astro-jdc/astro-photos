"""Ingest: linearisation, tone-curve inversion, metadata, manifests, licences."""

from __future__ import annotations

import json

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image

from astrostack.errors import LicenseViolation, UnsupportedFormatError
from astrostack.io.frame import Frame, FrameMetadata
from astrostack.io.loaders import (
    compute_airmass,
    detect_format,
    load_frame,
    pixel_scale_prior_arcsec,
    poisson_variance,
)
from astrostack.io.manifest import load_manifest
from astrostack.io.tone import estimate_residual_gamma, invert_tone_curve, srgb_to_linear
from astrostack.io.writers import asinh_stretch, checksum_arrays, write_result_fits
from astrostack.optional import have
from astrostack.rng import generator


# --------------------------------------------------------------------------
# Tone curve
# --------------------------------------------------------------------------
def test_srgb_inversion_matches_the_standard():
    """Exact, tabulated values from IEC 61966-2-1."""
    assert float(srgb_to_linear(np.array([0.0]))[0]) == pytest.approx(0.0)
    assert float(srgb_to_linear(np.array([1.0]))[0]) == pytest.approx(1.0)
    assert float(srgb_to_linear(np.array([0.5]))[0]) == pytest.approx(0.2140, abs=1e-3)
    # Monotone, which is what makes it invertible at all.
    x = np.linspace(0, 1, 64)
    assert np.all(np.diff(srgb_to_linear(x)) > 0)


def test_photon_transfer_recovers_a_known_gamma():
    """The estimator has to actually work, or the JPEG path is a lie.

    A synthetic Poisson sky with a gradient is put through a known power law
    and the exponent is recovered from the variance-versus-mean relation.
    """
    rng = generator(1234, "tone")
    h = w = 256
    _gy, gx = np.mgrid[0:h, 0:w]
    # Sky level varying by 6x across the frame: the estimator needs a range of
    # mean levels to fit a slope against.
    level = 200.0 + 1000.0 * (gx / (w - 1))
    linear = rng.poisson(level) / 4000.0

    for true_gamma in (1.6, 2.2, 3.0):
        encoded = np.clip(linear, 0, 1) ** (1.0 / true_gamma)
        est = estimate_residual_gamma(encoded, patch=24)
        assert est.converged, est.reason
        assert est.gamma == pytest.approx(true_gamma, rel=0.18), (true_gamma, est.gamma)


def test_tone_estimator_refuses_when_it_cannot_tell():
    """A flat, noiseless frame gives the estimator nothing; it must say so."""
    flat = np.full((128, 128), 0.4, dtype=np.float32)
    est = estimate_residual_gamma(flat)
    assert not est.converged
    assert est.gamma == 1.0
    assert est.reason


def test_invert_tone_curve_can_be_forced():
    x = np.linspace(0.01, 0.99, 64 * 64).reshape(64, 64).astype(np.float32)
    out, est = invert_tone_curve(x, assume_srgb=False, forced_gamma=2.2)
    assert est.gamma == 2.2
    assert np.allclose(out, np.clip(x, 0, 1) ** 2.2, atol=1e-5)


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------
def test_detect_format():
    assert detect_format("a.CR3") == "raw"
    assert detect_format("a.fits") == "fits"
    assert detect_format("a.TIF") == "tiff"
    assert detect_format("a.jpeg") == "jpeg"
    with pytest.raises(UnsupportedFormatError):
        detect_format("a.txt")


def test_fits_round_trip_keeps_data_linear(tmp_path):
    rng = generator(9, "fits")
    data = rng.normal(500.0, 20.0, (48, 48)).astype(np.float32)
    path = tmp_path / "x.fits"
    header = fits.Header({"EXPTIME": 120.0, "GAIN": 1.6, "RDNOISE": 3.2, "FILTER": "Ha"})
    fits.PrimaryHDU(data=data, header=header).writeto(path)

    frame = load_frame(path)
    assert np.allclose(frame.data, data)
    assert frame.meta.source_format == "fits"
    assert frame.meta.exposure_seconds == 120.0
    assert frame.meta.gain_e_per_adu == pytest.approx(1.6)
    assert frame.meta.filter_name == "Ha"
    assert not frame.meta.photometrically_unreliable
    assert frame.variance is not None


def test_jpeg_is_loaded_but_flagged(tmp_path):
    """Rule from section 8: a JPEG may contribute, but never silently."""
    rng = generator(3, "jpeg")
    linear = np.clip(rng.poisson(np.full((128, 128), 400.0)) / 1200.0, 0, 1)
    encoded = (np.clip(linear, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
    path = tmp_path / "x.jpg"
    Image.fromarray(encoded, mode="L").convert("RGB").save(path, quality=95)

    frame = load_frame(path)
    assert frame.meta.source_format == "jpeg"
    assert frame.meta.photometrically_unreliable is True
    assert "8-bit" in (frame.meta.unreliable_reason or "")
    assert frame.data.dtype == np.float32
    assert frame.extra["tone_curve"] is not None


def test_tiff_16bit_is_treated_as_linear(tmp_path):
    import tifffile

    data = (np.linspace(0, 60000, 64 * 64).reshape(64, 64)).astype(np.uint16)
    path = tmp_path / "x.tif"
    tifffile.imwrite(path, data)

    frame = load_frame(path)
    assert not frame.meta.photometrically_unreliable
    assert frame.meta.bit_depth == 16
    assert np.allclose(frame.data, data.astype(np.float32))


def test_8bit_tiff_is_flagged(tmp_path):
    import tifffile

    data = (np.linspace(0, 255, 64 * 64).reshape(64, 64)).astype(np.uint8)
    path = tmp_path / "x8.tif"
    tifffile.imwrite(path, data)
    frame = load_frame(path)
    assert frame.meta.photometrically_unreliable is True


def test_frame_rejects_a_colour_cube():
    with pytest.raises(ValueError, match="2-D"):
        Frame(frame_id="x", data=np.zeros((8, 8, 3)), meta=FrameMetadata(photo_id="x"))


@pytest.mark.skipif(not have("rawpy"), reason="rawpy not installed")
def test_raw_loader_is_importable():
    """The RAW path is exercised for import-time correctness only.

    A real RAW fixture would be a committed binary, which this suite does not
    do; the decode itself is LibRaw's responsibility and is covered upstream.
    """
    from astrostack.io.raw import CFA_CHANNELS, load_raw_planes

    assert set(CFA_CHANNELS) == {"R", "G", "G1", "G2", "B"}
    assert callable(load_raw_planes)


# --------------------------------------------------------------------------
# Derived metadata
# --------------------------------------------------------------------------
def test_pixel_scale_prior():
    # 3.76 um pixels behind 400 mm: 206.265 * 3.76 / 400 = 1.939 arcsec/px
    assert pixel_scale_prior_arcsec(400.0, 3.76) == pytest.approx(1.9389, rel=1e-3)
    # Derived from sensor width when the pitch is unknown.
    assert pixel_scale_prior_arcsec(400.0, None, 23.5, 6000) == pytest.approx(
        pixel_scale_prior_arcsec(400.0, 23.5 / 6000 * 1000)
    )
    # Binning 2 (a CFA plane at half resolution) doubles the scale.
    assert pixel_scale_prior_arcsec(400.0, 3.76, binning=2) == pytest.approx(
        2 * pixel_scale_prior_arcsec(400.0, 3.76)
    )
    assert pixel_scale_prior_arcsec(None, 3.76) is None


def test_diffraction_limit():
    """1.22 lambda/D — the wall section 5 says nothing can cross."""
    meta = FrameMetadata(photo_id="x", focal_length_mm=400.0, focal_ratio=5.0)
    assert meta.aperture_or_estimate_mm == pytest.approx(80.0)
    # 1.22 * 550e-9 / 0.08 rad = 1.73 arcsec
    assert meta.diffraction_limit_arcsec() == pytest.approx(1.73, rel=0.02)
    assert FrameMetadata(photo_id="y").diffraction_limit_arcsec() is None


def test_airmass_is_computed_and_degrades_gracefully():
    from datetime import UTC, datetime

    when = datetime(2024, 1, 15, 2, 0, tzinfo=UTC)
    airmass, alt, _ = compute_airmass(83.8, -5.4, when, 28.3, -16.5, 2400.0)
    assert alt is not None
    if airmass is not None:
        assert 1.0 <= airmass < 40.0

    # Missing inputs must return None, not a plausible-looking guess.
    assert compute_airmass(None, None, when, 28.3, -16.5)[0] is None
    assert compute_airmass(83.8, -5.4, None, 28.3, -16.5)[0] is None


def test_poisson_variance_model():
    data = np.full((16, 16), 1000.0, dtype=np.float32)
    var = poisson_variance(data, gain_e_per_adu=2.0, read_noise_e=6.0)
    # 1000/2 + (6/2)^2 = 509
    assert float(var[0, 0]) == pytest.approx(509.0, rel=1e-5)

    # Without a gain we still get a sane, positive, finite variance.
    fallback = poisson_variance(data + np.arange(16), None, None)
    assert np.all(fallback > 0) and np.all(np.isfinite(fallback))


# --------------------------------------------------------------------------
# Manifest and licences
# --------------------------------------------------------------------------
def _write_fits(path, value=1.0):
    fits.PrimaryHDU(data=np.full((8, 8), value, dtype=np.float32)).writeto(path)


def test_manifest_orders_by_photo_id(tmp_path):
    for name in ("zeta", "alpha", "mu"):
        _write_fits(tmp_path / f"{name}.fits")
    manifest = load_manifest(tmp_path)
    assert manifest.photo_ids() == ["alpha", "mu", "zeta"]


def test_manifest_reads_backend_metadata(tmp_path):
    _write_fits(tmp_path / "a.fits")
    payload = [
        {
            "path": "a.fits",
            "photo_id": "photo-1",
            "license": "CC-BY-SA-4.0",
            "attribution_name": "Ada",
            "focal_length_mm": 200.0,
            "allow_ai_training": False,
        }
    ]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))

    manifest = load_manifest(path)
    spec = manifest.inputs[0]
    assert spec.meta.photo_id == "photo-1"
    assert spec.meta.license == "CC-BY-SA-4.0"
    assert spec.meta.allow_ai_training is False
    assert manifest.attribution_rows()[0]["author"] == "Ada"


@pytest.mark.parametrize("code", ["CC-BY-ND-4.0", "CC-BY-NC-ND-4.0"])
def test_nd_licences_are_refused_not_degraded(tmp_path, code):
    """Rule 3 of the licence table: an ND photo cannot enter, full stop."""
    _write_fits(tmp_path / "a.fits")
    (tmp_path / "manifest.json").write_text(
        json.dumps([{"path": "a.fits", "photo_id": "p", "license": code}])
    )
    with pytest.raises(LicenseViolation):
        load_manifest(tmp_path / "manifest.json", strict_licenses=True)


def test_allow_derivatives_false_is_refused(tmp_path):
    _write_fits(tmp_path / "a.fits")
    _write_fits(tmp_path / "b.fits")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {"path": "a.fits", "photo_id": "p", "allow_derivatives_in_stacks": False},
                {"path": "b.fits", "photo_id": "q", "license": "CC0-1.0"},
            ]
        )
    )
    manifest = load_manifest(tmp_path / "manifest.json", strict_licenses=False)
    assert manifest.photo_ids() == ["q"]
    assert manifest.rejected[0]["photo_id"] == "p"
    assert "allow_derivatives_in_stacks" in manifest.rejected[0]["reason"]


def test_duplicate_photo_ids_are_rejected(tmp_path):
    from astrostack.errors import PipelineConfigError

    _write_fits(tmp_path / "a.fits")
    _write_fits(tmp_path / "b.fits")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [{"path": "a.fits", "photo_id": "same"}, {"path": "b.fits", "photo_id": "same"}]
        )
    )
    with pytest.raises(PipelineConfigError, match="duplicate"):
        load_manifest(tmp_path / "manifest.json")


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------
def test_write_result_fits_is_deterministic(tmp_path):
    from astrostack.align.platesolve import make_tangent_wcs
    from astrostack.pipelines.provenance import file_sha256

    rng = generator(11, "w")
    image = rng.normal(0, 1, (24, 24)).astype(np.float32)
    wcs = make_tangent_wcs(10.0, 20.0, 2.0, (24, 24))
    kwargs = {
        "image": image,
        "weight": np.ones_like(image),
        "uncertainty": np.ones_like(image),
        "wcs": wcs,
    }

    a = write_result_fits(tmp_path / "a.fits", **kwargs)
    b = write_result_fits(tmp_path / "b.fits", **kwargs)
    assert a == b
    assert file_sha256(tmp_path / "a.fits") == file_sha256(tmp_path / "b.fits")


def test_fits_headers_are_forced_to_ascii(tmp_path):
    """FITS headers are ASCII by standard; our prose is not.

    The Tier B label contains an em dash, and astropy refuses non-ASCII cards
    outright — so the provenance would have been silently kept out of the
    file, or the write would crash. Transliterate instead.
    """
    from astrostack.io.writers import ascii_safe

    assert ascii_safe("a \u2014 b") == "a -- b"
    assert ascii_safe("3 \u03c3 at \u03bb/D") == "3 sigma at lambda/D"
    assert ascii_safe("x" * 200, limit=10) == "x" * 10
    assert all(32 <= ord(c) < 127 for c in ascii_safe("caf\u00e9\u2026"))

    image = np.zeros((8, 8), dtype=np.float32)
    path = tmp_path / "unicode.fits"
    write_result_fits(
        path,
        image=image,
        header_cards={"NOTE": "AI-enhanced (wcs-burst, x2) \u2014 partly inferred"},
        history=["3 \u03c3 detection at \u03bb/D \u2014 measured"],
    )
    header = fits.getheader(path)
    assert "--" in header["NOTE"]
    assert any("sigma" in str(line) for line in header["HISTORY"])


def test_checksum_arrays_is_order_independent_and_sensitive():
    a = np.arange(9, dtype=np.float32).reshape(3, 3)
    b = a + 1
    assert checksum_arrays(x=a, y=b) == checksum_arrays(y=b, x=a)
    assert checksum_arrays(x=a) != checksum_arrays(x=b)
    assert checksum_arrays(x=a) != checksum_arrays(x=a.astype(np.float64))


def test_asinh_stretch_is_bounded_and_monotone():
    x = np.linspace(-5, 1000, 4096).reshape(64, 64)
    y = asinh_stretch(x)
    assert y.min() >= 0.0 and y.max() <= 1.0
    flat = y.ravel()[np.argsort(x.ravel())]
    assert np.all(np.diff(flat) >= -1e-6)


def test_frame_history_declares_flux_status():
    frame = Frame(frame_id="f", data=np.ones((4, 4)), meta=FrameMetadata(photo_id="f"))
    frame.note("stage.a", "did a thing", flux_preserving=True)
    frame.note("stage.b", "did another", flux_preserving=False)
    assert "[flux-preserving]" in frame.history[0]
    assert "[NOT-flux-preserving]" in frame.history[1]
    assert frame.summary()["history"] == frame.history
