r"""Drizzle — variable-pixel linear reconstruction.

Reference
---------
Fruchter, A. S. & Hook, R. N. 2002, *Drizzle: A Method for the Linear
Reconstruction of Undersampled Images*, PASP 114, 144 (arXiv:astro-ph/9808087).

Why it matters here (section 3 of the research note): drizzle was built for
WFPC2, whose PSF was undersampled by its pixel grid — *exactly* the situation
of a wide-field DSLR shot, where a 50 mm lens on 4 um pixels gives ~16
arcsec/pixel against a ~2 arcsec optical limit. Given inputs with genuine
sub-pixel diversity, drizzle recovers the sampling that aliasing destroyed.
Independent observers supply that diversity for free.

The algorithm
-------------
Each input pixel is shrunk about its centre by ``pixfrac``, the resulting
"drop" is mapped through the geometry onto the output grid, and its flux is
distributed to output pixels in proportion to the overlap area. Following
Fruchter & Hook eq. (1)-(2), for input pixel ``i`` and output pixel ``xy``::

    I_xy = sum_i ( a_xy,i * w_i * v_i ) / sum_i ( a_xy,i * w_i )
    W_xy = sum_i ( a_xy,i * w_i )

where ``a_xy,i`` is the overlap area fraction, ``w_i`` the input weight, and
``v_i = d_i * (A_out / A_in)`` the input value converted to the output pixel's
solid angle. That conversion is the ``s**2`` factor of the paper and it is what
makes the operation **flux conserving**: shrinking the output pixels by 2 in
each axis quarters ``v_i`` while quadrupling the number of output pixels a
drop lands in.

How the overlap area is computed here
-------------------------------------
The reference implementation clips the mapped quadrilateral against the output
pixel grid exactly, in C. This module instead evaluates the overlap integral
by **deterministic quadrature**: the drop is divided into ``subsample**2``
equal sub-drops on a regular grid, each carrying ``1/subsample**2`` of the
drop's flux and weight, and each deposited whole into the output pixel
containing its centre.

The consequences are worth stating precisely, because "approximate" and
"wrong" are not the same thing:

* **Flux conservation is exact at any ``subsample``**, not asymptotic. Every
  sub-drop's flux and its matching weight are deposited into the same output
  pixel, so numerator and denominator stay consistent and nothing is lost
  except drops that genuinely fall off the output grid.
* The *shape* of the effective drop kernel converges to the exact polygon
  overlap as ``subsample`` grows, with an O(1/subsample) error confined to
  output pixels straddling a drop boundary. ``subsample=5`` (the default) puts
  that below a percent for the scale ratios in this corpus.
* Sub-drop offsets are propagated through the **local Jacobian** of the
  geometric mapping rather than through a fresh spherical projection per
  sub-drop. Over a sub-pixel displacement the linearisation error is many
  orders of magnitude below the sampling error, and it makes the mapping cost
  independent of ``subsample``.

Weights are per-frame scalars times a per-pixel validity mask — never a
signal-dependent per-pixel inverse variance. A weight that varies *within* a
star (because the star is brighter, hence noisier, at its core) would turn the
weighted mean into a biased estimator and break flux conservation. This is why
drizzle uses exposure/throughput weights.
"""

from __future__ import annotations

import numpy as np

from astrostack.errors import RegistrationError
from astrostack.io.frame import Frame
from astrostack.logging import get_logger
from astrostack.stack.base import CoaddResult

__all__ = ["DrizzleAccumulator", "drizzle", "pixel_map"]

log = get_logger(__name__)


def pixel_map(frame: Frame, out_wcs, out_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Output pixel coordinates of every input pixel centre."""
    if frame.wcs is None:
        raise RegistrationError(f"{frame.frame_id}: drizzle needs a WCS")
    h, w = frame.shape
    yy, xx = np.mgrid[0:h, 0:w]
    sky = frame.wcs.pixel_to_world(xx.ravel().astype(float), yy.ravel().astype(float))
    xo, yo = out_wcs.world_to_pixel(sky)
    _ = out_shape
    return xo.reshape(h, w), yo.reshape(h, w)


def _jacobian(xo: np.ndarray, yo: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Local Jacobian of the input->output map and its determinant."""
    dxo_dy, dxo_dx = np.gradient(xo)
    dyo_dy, dyo_dx = np.gradient(yo)
    det = dxo_dx * dyo_dy - dxo_dy * dyo_dx
    return dxo_dx, dxo_dy, dyo_dx, dyo_dy, det


class DrizzleAccumulator:
    """Running numerator / denominator / variance for a drizzled output."""

    def __init__(self, shape: tuple[int, int]) -> None:
        self.shape = shape
        self.num = np.zeros(shape, dtype=np.float64)
        self.den = np.zeros(shape, dtype=np.float64)
        self.var_num = np.zeros(shape, dtype=np.float64)
        self.context = np.zeros(shape, dtype=np.int32)
        self.total_input_flux = 0.0
        self.total_deposited_flux = 0.0
        self.dropped_off_grid = 0

    def add_frame(
        self,
        frame: Frame,
        out_wcs,
        pixfrac: float = 0.7,
        subsample: int = 5,
        frame_weight: float = 1.0,
    ) -> dict[str, float]:
        oh, ow = self.shape
        xo, yo = pixel_map(frame, out_wcs, self.shape)
        dxdx, dxdy, dydx, dydy, det = _jacobian(xo, yo)

        area_ratio = np.abs(det)                       # A_in / A_out
        area_ratio = np.where(area_ratio > 1e-12, area_ratio, np.nan)
        values = frame.data.astype(np.float64) / area_ratio   # v_i = d_i * A_out/A_in

        valid = frame.good & np.isfinite(values) & np.isfinite(xo) & np.isfinite(yo)
        weights = np.where(valid, float(frame_weight), 0.0)
        values = np.where(valid, values, 0.0)
        variance = frame.effective_variance().astype(np.float64)
        variance = np.where(valid, variance / np.where(np.isfinite(area_ratio), area_ratio, 1.0) ** 2, 0.0)

        k = max(int(subsample), 1)
        # Sub-drop centres inside the shrunk pixel, in input-pixel units.
        offs = (np.arange(k, dtype=np.float64) + 0.5) / k - 0.5
        offs = offs * float(pixfrac)
        share = 1.0 / (k * k)

        self.total_input_flux += float(np.sum(frame.data.astype(np.float64)[valid]))
        deposited = 0.0

        for du in offs:
            for dv in offs:
                # Local linearisation: sub-pixel offsets go through the Jacobian.
                sx = xo + dxdx * du + dxdy * dv
                sy = yo + dydx * du + dydy * dv
                ix = np.rint(sx).astype(np.int64)
                iy = np.rint(sy).astype(np.int64)
                inside = valid & (ix >= 0) & (ix < ow) & (iy >= 0) & (iy < oh)
                if not inside.any():
                    self.dropped_off_grid += int(valid.sum())
                    continue
                flat = (iy[inside] * ow + ix[inside]).astype(np.int64)
                wv = weights[inside] * share
                np.add.at(self.num.reshape(-1), flat, wv * values[inside])
                np.add.at(self.den.reshape(-1), flat, wv)
                np.add.at(self.var_num.reshape(-1), flat, (wv**2) * variance[inside])
                np.add.at(self.context.reshape(-1), flat, 1)
                deposited += float(np.sum(values[inside] * share * np.abs(det[inside])))
                self.dropped_off_grid += int(valid.sum() - inside.sum())

        self.total_deposited_flux += deposited
        return {
            "frame_id": frame.frame_id,  # type: ignore[dict-item]
            "weight": float(frame_weight),
            "coverage": float(np.mean(valid)),
        }

    def finish(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        covered = self.den > 0
        image = np.divide(self.num, self.den, out=np.zeros_like(self.num), where=covered)
        var = np.divide(
            self.var_num, np.square(self.den), out=np.zeros_like(self.num), where=covered
        )
        return (
            image.astype(np.float32),
            self.den.astype(np.float32),
            np.sqrt(np.maximum(var, 0.0)).astype(np.float32),
        )


def _effective_drizzle_psf(
    frames: list[Frame],
    weights: np.ndarray,
    input_scales: list[float],
    out_scale: float,
    pixfrac: float,
    size: int = 33,
) -> np.ndarray | None:
    """Approximate effective PSF: input PSF rescaled, convolved with the drop.

    Ignores the (beneficial) narrowing that a well-dithered set produces, so it
    is a slight *over*-estimate of the output FWHM. Documented as such because
    an over-estimated PSF makes the matched filter conservative rather than
    over-confident.
    """
    from scipy.ndimage import zoom
    from scipy.signal import fftconvolve

    size = max(int(size) | 1, 5)
    acc = np.zeros((size, size), dtype=np.float64)
    total = 0.0
    for fr, wgt, in_scale in zip(frames, weights, input_scales, strict=True):
        if fr.psf is None or wgt <= 0:
            continue
        k = np.asarray(fr.psf.normalised(), dtype=np.float64)
        factor = in_scale / out_scale if out_scale > 0 else 1.0
        if abs(factor - 1.0) > 1e-3:
            k = zoom(k, factor, order=1)
            s = k.sum()
            if s <= 0:
                continue
            k = k / s
        drop_px = max(pixfrac * factor, 1e-3)
        n_box = max(int(np.ceil(drop_px)) | 1, 1)
        box = np.zeros((n_box, n_box), dtype=np.float64)
        box[:] = 1.0
        # Partial coverage of the outer ring of the box kernel.
        if n_box > 1:
            edge = (drop_px - (n_box - 2)) / 2.0
            box[0, :] = box[-1, :] = np.clip(edge, 0.0, 1.0)
            box[:, 0] = box[:, -1] = np.clip(edge, 0.0, 1.0)
        box /= box.sum()
        conv = fftconvolve(k, box, mode="full")
        conv = np.clip(conv, 0.0, None)
        s = conv.sum()
        if s <= 0:
            continue
        conv /= s
        buf = np.zeros((size, size), dtype=np.float64)
        cy, cx = size // 2, size // 2
        ky, kx = conv.shape
        y0, x0 = cy - ky // 2, cx - kx // 2
        ys, xs = max(y0, 0), max(x0, 0)
        ye, xe = min(y0 + ky, size), min(x0 + kx, size)
        if ye <= ys or xe <= xs:
            continue
        buf[ys:ye, xs:xe] = conv[ys - y0 : ye - y0, xs - x0 : xe - x0]
        acc += float(wgt) * buf
        total += float(wgt)
    if total <= 0 or acc.sum() <= 0:
        return None
    return (acc / acc.sum()).astype(np.float32)


def drizzle(
    frames: list[Frame],
    grid,
    pixfrac: float = 0.7,
    subsample: int = 5,
    weighting: str = "uniform",
) -> CoaddResult:
    """Drizzle frames (on their *native* grids) onto ``grid``.

    Parameters
    ----------
    frames
        Frames with a WCS, **not** pre-reprojected: drizzle does the
        resampling itself, in one flux-conserving step.
    grid
        An :class:`astrostack.align.register.OutputGrid`.
    pixfrac
        Drop shrink factor. 0.6-0.8 is the range the research note recommends
        for this kind of data: small enough to sharpen, large enough to avoid
        holes at realistic dither counts.
    weighting
        ``uniform`` or ``frame-inverse-variance`` (a *scalar* per frame).
    """
    if not frames:
        raise ValueError("drizzle needs at least one frame")
    ids = [f.frame_id for f in frames]
    if ids != sorted(ids):
        raise ValueError("frames must be sorted by frame_id before drizzling")
    if not 0.0 < pixfrac <= 1.0:
        raise ValueError(f"pixfrac must be in (0, 1], got {pixfrac}")

    acc = DrizzleAccumulator(grid.shape)
    weights = np.ones(len(frames), dtype=np.float64)
    if weighting == "frame-inverse-variance":
        for i, fr in enumerate(frames):
            sigma = fr.quality.noise_sigma or fr.quality.background_rms
            if not sigma or sigma <= 0:
                sigma = float(np.sqrt(np.nanmedian(fr.effective_variance()))) or 1.0
            weights[i] = 1.0 / (sigma * sigma)
        weights /= weights.max()
    elif weighting != "uniform":
        raise ValueError(f"unknown drizzle weighting {weighting!r}")

    per_frame = []
    for fr, wgt in zip(frames, weights, strict=True):
        per_frame.append(acc.add_frame(fr, grid.wcs, pixfrac, subsample, float(wgt)))

    image, weight, uncertainty = acc.finish()

    from astropy.wcs.utils import proj_plane_pixel_scales

    in_scales = [
        float(np.mean(proj_plane_pixel_scales(fr.wcs)) * 3600.0) if fr.wcs else grid.pixel_scale_arcsec
        for fr in frames
    ]
    psf = _effective_drizzle_psf(
        frames, weights, in_scales, grid.pixel_scale_arcsec, pixfrac
    )

    covered = weight > 0
    holes = float(1.0 - covered.mean())
    notes = [
        f"Fruchter & Hook (2002) drizzle, pixfrac={pixfrac}, subsample={subsample}x{subsample}",
        "flux conserving by construction: sub-drop flux and weight are deposited together",
    ]
    if holes > 0.02:
        notes.append(
            f"{holes:.1%} of the output grid received no drop. Raise pixfrac or lower the "
            "output resolution: holes mean the dither pattern does not support this grid"
        )

    return CoaddResult(
        image=image,
        weight=weight,
        uncertainty=uncertainty,
        psf=psf,
        wcs=grid.wcs,
        method="drizzle",
        n_frames=len(frames),
        flux_preserving=True,
        frame_weights={fr.frame_id: float(w) for fr, w in zip(frames, weights, strict=True)},
        metrics={
            "pixfrac": float(pixfrac),
            "subsample": int(subsample),
            "coverage_fraction": float(covered.mean()),
            "hole_fraction": holes,
            "output_pixel_scale_arcsec": float(grid.pixel_scale_arcsec),
            "oversample": float(grid.oversample),
            "dither_score": float(grid.dither_score),
            "per_frame": per_frame,
        },
        notes=notes,
    )
