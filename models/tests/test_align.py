"""Astrometry, PSF characterisation and WCS-driven registration."""

from __future__ import annotations

import numpy as np
import pytest

from astrostack.align import (
    ASTAPSolver,
    AstrometryNetSolver,
    NoOpSolver,
    build_solver,
    characterise_frame,
    cross_validate_registration,
    detect_sources,
    dither_diversity,
    gaussian_kernel,
    make_output_grid,
    make_tangent_wcs,
    moffat_kernel,
    reproject_frame,
)
from astrostack.errors import PlateSolveError, RegistrationError
from astrostack.io.frame import Frame, FrameMetadata
from astrostack.optional import have
from tests.synthetic import make_corpus


# --------------------------------------------------------------------------
# Plate solving
# --------------------------------------------------------------------------
def test_noop_solver_returns_the_frames_own_wcs(dithered_corpus):
    field, frames = dithered_corpus
    solved = NoOpSolver().solve_frame(frames[0])
    assert solved.wcs is not None
    assert solved.quality.is_plate_solved
    assert solved.quality.solver == "noop:from-frame"
    assert solved.quality.pixel_scale_arcsec == pytest.approx(2.0, rel=1e-3)
    assert solved.extra["plate_solution"]["ra_deg"] == pytest.approx(field.truth.ra_deg, abs=0.3)


def test_noop_solver_synthesises_from_a_scale_prior():
    """With no WCS it builds one from a centre plus the EXIF scale prior."""
    frame = Frame(
        frame_id="f",
        data=np.zeros((64, 64), dtype=np.float32),
        meta=FrameMetadata(photo_id="f", focal_length_mm=400.0, pixel_pitch_um=3.76),
    )
    frame.quality.pixel_scale_arcsec = 1.94
    solved = NoOpSolver(ra_deg=10.0, dec_deg=41.0).solve_frame(frame)
    assert solved.wcs is not None
    assert solved.extra["plate_solution"]["ra_deg"] == pytest.approx(10.0, abs=1e-3)
    assert solved.quality.pixel_scale_arcsec == pytest.approx(1.94, rel=1e-3)


def test_noop_solver_refuses_when_it_has_nothing_to_go_on():
    frame = Frame(
        frame_id="f", data=np.zeros((32, 32), dtype=np.float32), meta=FrameMetadata(photo_id="f")
    )
    with pytest.raises(PlateSolveError, match="ra_deg"):
        NoOpSolver().solve_frame(frame)


def test_subprocess_solvers_fail_with_an_actionable_message():
    """No binary installed is a normal outcome, and must say what to install."""
    frame = Frame(
        frame_id="f", data=np.zeros((32, 32), dtype=np.float32), meta=FrameMetadata(photo_id="f")
    )
    for solver, hint in (
        (AstrometryNetSolver(binary="definitely-not-installed-xyz"), "astrometry.net"),
        (ASTAPSolver(binary="definitely-not-installed-xyz"), "ASTAP"),
    ):
        assert not solver.available()
        with pytest.raises(PlateSolveError) as exc:
            solver.solve(frame)
        assert hint in str(exc.value)


def test_solver_factory():
    assert isinstance(build_solver("noop"), NoOpSolver)
    assert isinstance(build_solver("astap"), ASTAPSolver)
    assert isinstance(build_solver("astrometry.net"), AstrometryNetSolver)
    with pytest.raises(PlateSolveError, match="unknown solver"):
        build_solver("magic")


def test_scale_prior_narrows_the_search():
    from astrostack.align.platesolve import ScalePrior, scale_prior_from_frame

    frame = Frame(
        frame_id="f", data=np.zeros((32, 32), dtype=np.float32), meta=FrameMetadata(photo_id="f")
    )
    assert scale_prior_from_frame(frame) is None
    frame.quality.pixel_scale_arcsec = 2.0
    prior = scale_prior_from_frame(frame, tolerance=0.25)
    assert isinstance(prior, ScalePrior)
    assert prior.low == pytest.approx(1.5) and prior.high == pytest.approx(2.5)


# --------------------------------------------------------------------------
# Source detection and PSF
# --------------------------------------------------------------------------
def test_detection_finds_the_planted_stars():
    field = make_corpus(
        n_frames=1, shape=(160, 160), n_stars=20, seed=41,
        fwhm_pixels=3.0, sky_level=200.0, dither_pixels=0.0,
        flux_range=(5000.0, 40000.0),
    )  # fmt: skip
    catalog = detect_sources(field.frames[0].data, threshold_sigma=5.0)
    assert len(catalog) >= 15

    # Every detection must sit on a real star, within a pixel or two.
    truth = field.truth.positions
    for y, x in zip(catalog.y, catalog.x, strict=True):
        d2 = ((truth[:, 0] - y) ** 2 + (truth[:, 1] - x) ** 2).min()
        assert np.sqrt(d2) < 3.0


def test_measured_fwhm_tracks_the_truth():
    for true_fwhm in (2.5, 4.0, 6.0):
        field = make_corpus(
            n_frames=1, shape=(192, 192), n_stars=30, seed=42,
            fwhm_pixels=true_fwhm, sky_level=150.0, dither_pixels=0.0,
            flux_range=(8000.0, 60000.0), psf_shape="gaussian",
        )  # fmt: skip
        out = characterise_frame(field.frames[0], psf_model="gaussian")
        assert out.quality.fwhm_pixels == pytest.approx(true_fwhm, rel=0.25), (
            true_fwhm, out.quality.fwhm_pixels
        )
        assert out.quality.fwhm_arcsec == pytest.approx(out.quality.fwhm_pixels * 2.0)
        assert out.psf is not None
        assert float(out.psf.normalised().sum()) == pytest.approx(1.0, abs=1e-5)


def test_default_psf_model_is_analytic_and_consistent():
    """An analytic Moffat beats a sparse-field ePSF, and never varies per frame.

    A corpus characterised two different ways is worse than one characterised
    consistently, so the default must never fall back on some frames only.
    """
    field = make_corpus(
        n_frames=4, shape=(160, 160), n_stars=12, seed=61,
        fwhm_pixels=3.0, sky_level=200.0, dither_pixels=0.0,
    )  # fmt: skip
    sources = {characterise_frame(f).extra["psf_source"] for f in field.frames}
    assert sources == {"analytic"}


def test_psf_field_map_is_produced():
    field = make_corpus(
        n_frames=1, shape=(192, 192), n_stars=60, seed=43,
        fwhm_pixels=3.5, sky_level=150.0, dither_pixels=0.0,
        flux_range=(8000.0, 60000.0),
    )  # fmt: skip
    out = characterise_frame(field.frames[0], field_grid=(3, 3), psf_model="gaussian")
    assert out.psf.field_fwhm is not None
    assert out.psf.field_fwhm.shape == (3, 3)
    # A uniform-PSF frame must NOT be flagged as field varying.
    assert not out.psf.is_field_varying
    assert out.extra["psf_field_varying"] is False


def test_characterise_survives_an_empty_field():
    field = make_corpus(n_frames=1, shape=(96, 96), n_stars=0, seed=44, empty=True)
    out = characterise_frame(field.frames[0])
    assert out.quality.star_count == 0
    assert any("no sources" in h for h in out.history)


def test_kernels_are_unit_sum_and_ordered_by_width():
    from astrostack.metrics.quality import noise_equivalent_fwhm

    for fwhm in (2.0, 4.0, 8.0):
        g = gaussian_kernel(fwhm)
        m = moffat_kernel(fwhm)
        assert float(g.sum()) == pytest.approx(1.0, abs=1e-6)
        assert float(m.sum()) == pytest.approx(1.0, abs=1e-6)
        assert g.shape[0] % 2 == 1
        assert noise_equivalent_fwhm(g) == pytest.approx(fwhm, rel=0.05)

    assert noise_equivalent_fwhm(gaussian_kernel(2.0)) < noise_equivalent_fwhm(gaussian_kernel(5.0))


def test_elliptical_kernel_is_actually_elliptical():
    k = gaussian_kernel(4.0, size=31, ecc=0.8, theta=0.0)
    profile_x = k[15, :]
    profile_y = k[:, 15]
    assert np.sum(profile_x > 0.5 * profile_x.max()) != np.sum(
        profile_y > 0.5 * profile_y.max()
    )


# --------------------------------------------------------------------------
# Output grid and dither
# --------------------------------------------------------------------------
def test_dither_diversity_measures_what_it_claims():
    dithered = make_corpus(n_frames=9, shape=(96, 96), seed=45, dither_pixels=1.5)
    undithered = make_corpus(n_frames=9, shape=(96, 96), seed=45, dither_pixels=0.0)
    assert dither_diversity(dithered.frames) > 0.4
    assert dither_diversity(undithered.frames) < 0.4


def test_output_grid_refuses_to_oversample_without_dither():
    """The guard against selling interpolation as super-resolution."""
    undithered = make_corpus(n_frames=8, shape=(96, 96), seed=46, dither_pixels=0.0)
    grid = make_output_grid(undithered.frames, max_oversample=2.0)
    assert grid.oversample == 1.0
    assert grid.pixel_scale_arcsec == pytest.approx(2.0, rel=1e-3)

    dithered = make_corpus(n_frames=9, shape=(96, 96), seed=47, dither_pixels=1.5)
    fine = make_output_grid(dithered.frames, max_oversample=2.0)
    assert fine.oversample > 1.0
    assert fine.pixel_scale_arcsec < 2.0


def test_output_grid_covers_every_footprint(dithered_corpus):
    _, frames = dithered_corpus
    grid = make_output_grid(frames)
    for frame in frames:
        corners = frame.wcs.pixel_to_world(
            np.array([0.0, frame.shape[1] - 1.0]), np.array([0.0, frame.shape[0] - 1.0])
        )
        x, y = grid.wcs.world_to_pixel(corners)
        assert np.all(x > -2) and np.all(x < grid.shape[1] + 2)
        assert np.all(y > -2) and np.all(y < grid.shape[0] + 2)


def test_output_grid_needs_a_wcs():
    field = make_corpus(n_frames=2, shape=(32, 32), seed=48, with_wcs=False)
    with pytest.raises(RegistrationError, match="no frame has a WCS"):
        make_output_grid(field.frames)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def bright_dithered():
    """A dithered corpus bright enough to measure centroids on, per frame.

    The shared ``dithered_corpus`` fixture uses a realistic luminosity
    function, so a single frame yields only a handful of detections — fine for
    stacking tests, not enough to measure a registration RMS from.
    """
    from tests.synthetic import subtract_known_sky

    field = make_corpus(
        n_frames=5, shape=(160, 160), n_stars=30, seed=51,
        fwhm_pixels=3.0, sky_level=200.0, dither_pixels=1.5,
        flux_range=(20000.0, 120000.0),
    )  # fmt: skip
    return field, subtract_known_sky(field)


def test_registration_puts_stars_at_the_same_place(bright_dithered):
    """The whole point of the WCS path: sub-pixel alignment, analytically.

    Measured by cross-correlating each registered frame against the first: if
    the geometry is right the correlation peaks at zero shift. This is a
    stronger test than matching source lists, which can be defeated by two
    similarly-bright stars swapping rank between frames.
    """
    _field, frames = bright_dithered
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    registered = [reproject_frame(f, grid, method="adaptive") for f in frames]

    def peak_shift(a, b):
        fa = np.fft.rfft2(np.nan_to_num(a - np.median(a)))
        fb = np.fft.rfft2(np.nan_to_num(b - np.median(b)))
        corr = np.fft.irfft2(fa * np.conjugate(fb), s=a.shape)
        iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
        dy = iy if iy < a.shape[0] // 2 else iy - a.shape[0]
        dx = ix if ix < a.shape[1] // 2 else ix - a.shape[1]
        return dy, dx

    for other in registered[1:]:
        dy, dx = peak_shift(other.data, registered[0].data)
        assert (dy, dx) == (0, 0), f"registered frames are offset by ({dy}, {dx}) px"

    # And the unregistered frames really were offset, so the test has teeth.
    raw_offsets = {peak_shift(f.data, frames[0].data) for f in frames[1:]}
    assert raw_offsets != {(0, 0)}, "the input corpus carries no dither to correct"


def test_registration_renormalises_the_variance(dithered_corpus):
    """Resampling smooths the noise; the declared sigma must follow.

    Before this renormalisation existed, a frame reprojected with
    ``reproject_adaptive`` kept its *pre-resampling* sigma — more than twice
    the noise actually present — which silently mis-weighted every coadd.
    """
    from astrostack.robust import robust_sigma

    _, frames = dithered_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    out = reproject_frame(frames[0], grid, method="adaptive")

    covered = ~out.mask
    measured = robust_sigma(out.data, mask=out.mask)
    declared = float(np.sqrt(np.median(out.effective_variance()[covered])))
    assert 0.7 < declared / measured < 1.4, (declared, measured)
    assert out.quality.noise_sigma == pytest.approx(measured, rel=0.2)
    assert out.extra["variance_rescale"] < 1.0, "resampling must lower the per-pixel variance"


def test_cross_validation_accepts_a_good_wcs_and_rejects_a_bad_one(bright_dithered):
    _, frames = bright_dithered
    good = cross_validate_registration(frames[1], frames[0], frames[1].wcs, max_rms_px=1.5)
    assert good.accepted, good.reason
    assert good.rms_px is not None and good.rms_px < 1.5

    # A WCS shifted by 8 arcsec (4 px) is a wrong solution and must be caught.
    bad_wcs = make_tangent_wcs(
        frames[1].extra.get("ra", 83.822) + 0.01, -5.391, 2.0, frames[1].shape
    )
    bad = cross_validate_registration(frames[1], frames[0], bad_wcs, max_rms_px=1.5)
    assert not bad.accepted
    assert "exceeds" in bad.reason or "min_matches" in bad.reason


def test_reproject_requires_a_wcs():
    field = make_corpus(n_frames=2, shape=(32, 32), seed=49)
    grid = make_output_grid(field.frames, pixel_scale_arcsec=2.0)
    naked = field.frames[0].copy_with(field.frames[0].data, wcs=None)
    with pytest.raises(RegistrationError, match="without a WCS"):
        reproject_frame(naked, grid)


def test_unknown_reprojection_method_is_refused(dithered_corpus):
    _, frames = dithered_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    with pytest.raises(RegistrationError, match="unknown reprojection"):
        reproject_frame(frames[0], grid, method="magic")


@pytest.mark.skipif(not have("astroalign"), reason="astroalign not installed")
def test_astroalign_fallback_recovers_the_dither(bright_dithered):
    """The fallback for frames the solver could not solve."""
    from astrostack.align import astroalign_transform

    _, frames = bright_dithered
    matrix, n_matched = astroalign_transform(frames[1], frames[0])
    assert matrix.shape == (3, 3)
    assert n_matched >= 3
    # A pure dither is a translation: the linear part must be near identity.
    assert np.allclose(matrix[:2, :2], np.eye(2), atol=0.05)
