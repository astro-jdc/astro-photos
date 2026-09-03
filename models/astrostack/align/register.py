"""Registration onto a common output grid.

Primary path: **WCS-driven reprojection**. A plate solution gives geometric
registration to a small fraction of a pixel *analytically*, which is more
accurate than any learned optical flow (section 1, "critical caveat for
transfer"). We use :mod:`reproject`:

``adaptive``
    DeForest (2004) anti-aliased adaptive resampling, with ``conserve_flux``.
    The right default when the scale changes substantially, which it always
    does in a heterogeneous corpus.
``exact``
    Montage-style exact spherical-polygon overlap. Strictly flux conserving,
    slower. Use it when photometry matters more than speed.
``interp``
    SWarp-style interpolation. Fast, and *not* strictly flux conserving; it is
    offered but the frame is marked accordingly.

Fallback path: **astroalign** (Beroiz et al. 2020) triangle-similarity
matching, for frames the solver could not solve. It yields an affine transform
in pixel space, from which we synthesise a WCS by composing with the
reference frame's solution.

Both paths are cross-validated against each other when both are available:
if the two disagree by more than ``max_rms_px``, the frame is rejected rather
than silently smeared into the coadd.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from astropy.wcs import WCS

from astrostack.align.platesolve import make_tangent_wcs
from astrostack.align.stars import detect_sources
from astrostack.errors import RegistrationError
from astrostack.io.frame import Frame
from astrostack.logging import get_logger
from astrostack.optional import require
from astrostack.robust import robust_sigma

__all__ = [
    "OutputGrid",
    "RegistrationReport",
    "astroalign_transform",
    "cross_validate_registration",
    "dither_diversity",
    "make_output_grid",
    "reproject_frame",
]

log = get_logger(__name__)

FLUX_CONSERVING_METHODS = {"exact": True, "adaptive": True, "interp": False}


@dataclass(slots=True)
class OutputGrid:
    """The common tangent plane every frame is resampled onto."""

    wcs: WCS
    shape: tuple[int, int]
    pixel_scale_arcsec: float
    oversample: float
    dither_score: float
    contributing_scales: list[float] = field(default_factory=list)

    def describe(self) -> dict[str, object]:
        return {
            "shape": list(self.shape),
            "pixel_scale_arcsec": self.pixel_scale_arcsec,
            "oversample": self.oversample,
            "dither_score": self.dither_score,
            "contributing_scales_arcsec": self.contributing_scales,
        }


def dither_diversity(frames: list[Frame], reference: WCS | None = None) -> float:
    """Score in [0, 1] for how well the frames sample the sub-pixel plane.

    Drizzle and multi-frame SR only recover aliased detail when the inputs
    carry genuine sub-pixel diversity (section 5, "recovering aliased
    detail"). Independent observers usually supply it for free — but it has to
    be *measured* before the output grid is made finer than the inputs, or the
    pipeline is just interpolating and calling it super-resolution.

    Method: project a fixed sky point into every frame's pixel grid, take the
    fractional parts, and measure how uniformly the resulting points fill the
    unit square using bin occupancy. 1.0 = every bin hit.
    """
    solved = [f for f in frames if f.wcs is not None]
    if len(solved) < 2:
        return 0.0
    ref = reference or solved[0].wcs
    assert ref is not None
    h, w = solved[0].shape
    sky = ref.pixel_to_world(w / 2.0, h / 2.0)

    phases = []
    for f in solved:
        try:
            x, y = f.wcs.world_to_pixel(sky)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            continue
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        phases.append((float(x) % 1.0, float(y) % 1.0))
    if len(phases) < 2:
        return 0.0

    pts = np.asarray(phases)
    k = max(2, int(np.ceil(np.sqrt(len(pts)))))
    bins = np.zeros((k, k), dtype=bool)
    ix = np.clip((pts[:, 0] * k).astype(int), 0, k - 1)
    iy = np.clip((pts[:, 1] * k).astype(int), 0, k - 1)
    bins[iy, ix] = True
    return float(bins.sum()) / float(min(len(pts), k * k))


def _footprint_corners(frame: Frame) -> np.ndarray:
    """Sky coordinates (deg) of the four corners of a solved frame."""
    h, w = frame.shape
    xs = np.array([0.0, w - 1.0, w - 1.0, 0.0])
    ys = np.array([0.0, 0.0, h - 1.0, h - 1.0])
    world = frame.wcs.pixel_to_world(xs, ys)  # type: ignore[union-attr]
    return np.column_stack([world.ra.deg, world.dec.deg])


def make_output_grid(
    frames: list[Frame],
    pixel_scale_arcsec: float | None = None,
    scale_percentile: float = 25.0,
    max_oversample: float = 2.0,
    pad_fraction: float = 0.02,
    max_pixels: int = 64_000_000,
) -> OutputGrid:
    """Choose the common output tangent plane.

    The scale is taken as a low percentile of the contributing scales (i.e.
    biased towards the *sharper* contributors), then optionally divided by an
    oversampling factor that is granted **only** if the measured dither
    diversity justifies it. This is the guard against dressing interpolation
    up as super-resolution.
    """
    solved = [f for f in frames if f.wcs is not None]
    if not solved:
        raise RegistrationError("no frame has a WCS; cannot build an output grid")

    from astropy.wcs.utils import proj_plane_pixel_scales

    scales = [float(np.mean(proj_plane_pixel_scales(f.wcs)) * 3600.0) for f in solved]  # type: ignore[arg-type]
    base_scale = pixel_scale_arcsec or float(np.percentile(scales, scale_percentile))

    score = dither_diversity(solved)
    if pixel_scale_arcsec is not None:
        oversample = 1.0
    elif score >= 0.7:
        oversample = min(2.0, max_oversample)
    elif score >= 0.4:
        oversample = min(1.5, max_oversample)
    else:
        oversample = 1.0
    out_scale = base_scale / oversample

    corners = np.vstack([_footprint_corners(f) for f in solved])
    # Robust centre: mean of the unit vectors, so RA wrap-around is a non-issue.
    ra = np.deg2rad(corners[:, 0])
    dec = np.deg2rad(corners[:, 1])
    vec = np.column_stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])
    mean_vec = vec.mean(axis=0)
    mean_vec /= np.linalg.norm(mean_vec)
    centre_dec = float(np.degrees(np.arcsin(np.clip(mean_vec[2], -1.0, 1.0))))
    centre_ra = float(np.degrees(np.arctan2(mean_vec[1], mean_vec[0]))) % 360.0

    probe = make_tangent_wcs(centre_ra, centre_dec, out_scale, (16, 16))
    from astropy.coordinates import SkyCoord

    sky = SkyCoord(ra=corners[:, 0], dec=corners[:, 1], unit="deg")
    px, py = probe.world_to_pixel(sky)
    span_x = float(np.nanmax(px) - np.nanmin(px))
    span_y = float(np.nanmax(py) - np.nanmin(py))
    width = int(np.ceil(span_x * (1.0 + pad_fraction))) | 1
    height = int(np.ceil(span_y * (1.0 + pad_fraction))) | 1
    width, height = max(width, 8), max(height, 8)

    if width * height > max_pixels:
        shrink = np.sqrt(width * height / max_pixels)
        out_scale *= shrink
        width = max(8, int(width / shrink))
        height = max(8, int(height / shrink))
        oversample /= shrink
        log.warning("output_grid_downscaled", shrink=shrink, pixels=width * height)

    wcs = make_tangent_wcs(centre_ra, centre_dec, out_scale, (height, width))
    return OutputGrid(
        wcs=wcs,
        shape=(height, width),
        pixel_scale_arcsec=out_scale,
        oversample=oversample,
        dither_score=score,
        contributing_scales=[round(s, 5) for s in scales],
    )


def reproject_frame(
    frame: Frame,
    grid: OutputGrid,
    method: str = "adaptive",
    conserve_flux: bool = True,
    kernel: str = "gaussian",
    boundary_mode: str = "strict",
) -> Frame:
    """Resample one frame onto the output grid.

    Returns a frame whose ``data``, ``variance`` and ``mask`` all live on the
    output grid, with the coverage footprint stored in ``extra['footprint']``.
    """
    if frame.wcs is None:
        raise RegistrationError(f"{frame.frame_id}: cannot reproject without a WCS")
    method = method.lower()
    if method not in FLUX_CONSERVING_METHODS:
        raise RegistrationError(f"unknown reprojection method {method!r}")

    from reproject import reproject_adaptive, reproject_exact, reproject_interp

    data = np.where(frame.good, frame.data, np.nan).astype(np.float64)
    args = ((data, frame.wcs),)
    kwargs = {"output_projection": grid.wcs, "shape_out": grid.shape}

    if method == "adaptive":
        out, footprint = reproject_adaptive(
            *args, **kwargs, conserve_flux=conserve_flux, kernel=kernel,
            boundary_mode=boundary_mode, bad_value_mode="ignore",
        )  # fmt: skip
    elif method == "exact":
        out, footprint = reproject_exact(*args, **kwargs)
    else:
        out, footprint = reproject_interp(*args, **kwargs)

    var = frame.effective_variance().astype(np.float64)
    # Variance transforms with the square of the resampling kernel. Reprojecting
    # the variance directly is the standard first-order approximation; it ignores
    # the covariance the resampling introduces between neighbouring output
    # pixels, which is why the coadd's noise is estimated empirically too.
    var_out, _ = reproject_interp((var, frame.wcs), grid.wcs, shape_out=grid.shape)
    if conserve_flux and method == "adaptive":
        ratio = (grid.pixel_scale_arcsec / max(frame.quality.pixel_scale_arcsec or grid.pixel_scale_arcsec, 1e-9)) ** 2
        var_out = var_out * (ratio**2)

    covered = np.isfinite(out) & (np.asarray(footprint) > 0.5)
    filled = np.where(covered, out, 0.0).astype(np.float32)
    var_resampled = np.where(covered, np.nan_to_num(var_out, nan=0.0), 0.0).astype(np.float32)

    # Resampling *smooths* the noise: the per-pixel variance of the output is
    # substantially lower than the propagated input variance (at the price of
    # covariance between neighbours, which no scalar can express). Leaving the
    # pre-resampling sigma in place would be a large, silent error in every
    # downstream weight, so the variance map is renormalised to the noise that
    # is actually measurable in the output.
    measured_sigma = robust_sigma(filled, mask=~covered)
    scale_factor = 1.0
    if np.isfinite(measured_sigma) and measured_sigma > 0:
        median_var = float(np.median(var_resampled[covered])) if covered.any() else 0.0
        if median_var > 0:
            scale_factor = float(measured_sigma**2 / median_var)
            var_resampled = (var_resampled * scale_factor).astype(np.float32)

    out_frame = frame.copy_with(
        filled,
        wcs=grid.wcs,
        variance=var_resampled,
        mask=~covered,
        saturated=None,
        background=None,
    )
    if np.isfinite(measured_sigma) and measured_sigma > 0:
        out_frame.quality = frame.quality.model_copy(
            update={"noise_sigma": float(measured_sigma), "background_rms": float(measured_sigma)}
        )
    out_frame.extra = dict(frame.extra)
    out_frame.extra["footprint"] = float(covered.mean())
    out_frame.extra["reproject_method"] = method
    out_frame.extra["variance_rescale"] = scale_factor
    out_frame.note(
        "align.register",
        f"reproject_{method} onto {grid.shape} @ {grid.pixel_scale_arcsec:.3f}\"/px "
        f"(coverage {covered.mean():.1%}, sigma {measured_sigma:.4g}, "
        f"variance rescaled x{scale_factor:.3f}; neighbour covariance not tracked)",
        flux_preserving=FLUX_CONSERVING_METHODS[method] and conserve_flux,
    )
    return out_frame


@dataclass(slots=True)
class RegistrationReport:
    """Cross-validation of the WCS path against the astroalign path."""

    frame_id: str
    method: str
    rms_px: float | None
    n_matched: int
    accepted: bool
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "method": self.method,
            "alignment_rms_px": self.rms_px,
            "n_matched": self.n_matched,
            "accepted": self.accepted,
            "reason": self.reason,
        }


def astroalign_transform(
    frame: Frame,
    reference: Frame,
    max_control_points: int = 60,
    detection_sigma: float = 5.0,
) -> tuple[np.ndarray, int]:
    """Affine transform mapping ``frame`` pixels onto ``reference`` pixels.

    Returns ``(3x3 matrix, n_matched)``. Raises
    :class:`~astrostack.errors.RegistrationError` when the triangle matching
    fails, which is the honest outcome for a frame with too few stars.
    """
    aa = require("astroalign")
    try:
        transform, (src_list, _dst_list) = aa.find_transform(
            np.asarray(frame.data, dtype=np.float32),
            np.asarray(reference.data, dtype=np.float32),
            max_control_points=max_control_points,
            detection_sigma=detection_sigma,
        )
    except Exception as exc:
        raise RegistrationError(f"{frame.frame_id}: astroalign failed: {exc}") from exc
    return np.asarray(transform.params, dtype=np.float64), len(src_list)


def wcs_from_affine(reference_wcs: WCS, matrix: np.ndarray, shape: tuple[int, int]) -> WCS:
    """Compose a reference WCS with a pixel-space affine map.

    ``matrix`` maps *this* frame's pixels to *reference* pixels. The resulting
    WCS is a linear approximation: it carries no distortion terms, so a frame
    registered this way is marked lower quality than a plate-solved one.
    """
    h, w = shape
    cx, cy = w / 2.0, h / 2.0
    ref_x, ref_y = matrix @ np.array([cx, cy, 1.0])[:3]
    centre = reference_wcs.pixel_to_world(ref_x, ref_y)

    lin = matrix[:2, :2]
    ref_cd = reference_wcs.pixel_scale_matrix
    cd = ref_cd @ lin

    wcs = WCS(naxis=2)
    wcs.wcs.ctype = list(reference_wcs.wcs.ctype)
    wcs.wcs.crpix = [cx + 1.0, cy + 1.0]
    wcs.wcs.crval = [float(centre.ra.deg), float(centre.dec.deg)]
    wcs.wcs.cd = cd
    wcs.wcs.radesys = "ICRS"
    wcs.wcs.equinox = 2000.0
    wcs.pixel_shape = (w, h)
    return wcs


def cross_validate_registration(
    frame: Frame,
    reference: Frame,
    candidate_wcs: WCS,
    max_rms_px: float = 1.5,
    min_matches: int = 8,
    detection_sigma: float = 5.0,
) -> RegistrationReport:
    """Check a WCS registration against independent star matching.

    Detects sources in both frames, maps this frame's sources through
    ``candidate_wcs`` into the reference frame's pixel grid, and measures the
    RMS separation to the nearest reference source. A large RMS means the
    plate solution is wrong (mirrored field, wrong index scale, mis-identified
    field) and the frame must not be coadded.
    """
    cat = detect_sources(frame.data, threshold_sigma=detection_sigma)
    ref_cat = detect_sources(reference.data, threshold_sigma=detection_sigma)
    if len(cat) < min_matches or len(ref_cat) < min_matches or reference.wcs is None:
        return RegistrationReport(
            frame.frame_id, "wcs", None, 0, True, "too few sources to cross-validate; accepted on trust"
        )

    sky = candidate_wcs.pixel_to_world(cat.x, cat.y)
    px, py = reference.wcs.world_to_pixel(sky)
    ok = np.isfinite(px) & np.isfinite(py)
    if ok.sum() < min_matches:
        return RegistrationReport(frame.frame_id, "wcs", None, 0, False, "projection produced no finite positions")

    dx = px[ok][:, None] - ref_cat.x[None, :]
    dy = py[ok][:, None] - ref_cat.y[None, :]
    d2 = dx * dx + dy * dy
    nearest = np.sqrt(d2.min(axis=1))
    # Use the median-based core of the distribution: unmatched sources (real
    # transients, satellites, edge effects) must not dominate the statistic.
    core = nearest[nearest < max(3.0 * max_rms_px, np.median(nearest) * 3.0 + 1.0)]
    if core.size < min_matches:
        return RegistrationReport(
            frame.frame_id, "wcs", float(np.median(nearest)), int(core.size), False,
            "fewer than min_matches sources fall near a reference source",
        )  # fmt: skip
    rms = float(np.sqrt(np.mean(core**2)))
    accepted = rms <= max_rms_px
    return RegistrationReport(
        frame.frame_id, "wcs", rms, int(core.size), accepted,
        "ok" if accepted else f"alignment RMS {rms:.2f}px exceeds {max_rms_px}px",
    )  # fmt: skip
