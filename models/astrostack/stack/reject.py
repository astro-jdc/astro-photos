"""Robust per-pixel rejection: aeroplanes, satellites, cosmic rays.

Two complementary mechanisms, because they catch different things.

**Statistical rejection** (``sigma_clip_mask``, ``percentile_clip_mask``)
compares each frame's pixel with the robust central value of the stack. It
catches anything that is transient and bright: cosmic rays, hot pixels that
survived calibration, a car headlight.

**Morphological trail rejection** (``trail_mask``) takes the *high* outliers
of the statistical pass, labels connected components, and flags the ones that
are long and thin. An aeroplane or a Starlink train is a bright, highly
elongated, one-frame-only feature — a shape signature no pixel-wise test can
express. Components are dilated before rejection because the faint ends of a
trail sit below the pixel threshold but still bias the mean.

Rejection is asymmetric on purpose: ``sigma_high`` defaults tighter than
``sigma_low``. Real astronomical signal is positive and *persistent* across
frames, so an isolated positive outlier is nearly always an artefact, whereas
an isolated negative outlier is usually just noise.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

__all__ = ["RejectionResult", "combined_rejection", "percentile_clip_mask", "sigma_clip_mask", "trail_mask"]


@dataclass(slots=True)
class RejectionResult:
    """Per-frame boolean mask of pixels to drop, plus a breakdown."""

    rejected: np.ndarray  # (N, H, W) bool, True = drop
    n_sigma_clipped: int
    n_trail: int
    per_frame_fraction: np.ndarray

    def describe(self) -> dict[str, object]:
        return {
            "n_sigma_clipped": self.n_sigma_clipped,
            "n_trail": self.n_trail,
            "per_frame_fraction": [round(float(v), 8) for v in self.per_frame_fraction],
        }


#: Croux & Rousseeuw (1992) finite-sample correction factors for the MAD.
#: Without them the MAD of 8 samples underestimates sigma by 11%, which turns
#: a nominal 3-sigma clip into a 2.7-sigma clip and rejects ~3% of a pure-noise
#: stack. Those rejected pixels then get filled from their neighbours, and the
#: coadd ends up *noisier* than doing nothing — clipping actively harming the
#: result while appearing to protect it.
_MAD_SMALL_SAMPLE = {
    2: 1.196, 3: 1.495, 4: 1.363, 5: 1.206,
    6: 1.200, 7: 1.140, 8: 1.129, 9: 1.107,
}  # fmt: skip


def _mad_correction(n: int) -> float:
    """Finite-sample bias correction for a MAD computed from ``n`` values."""
    if n < 2:
        return 1.0
    if n in _MAD_SMALL_SAMPLE:
        return _MAD_SMALL_SAMPLE[n]
    return float(n) / (float(n) - 0.8)


def _robust_centre_scale(
    cube: np.ndarray, good: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    masked = np.where(good, cube, np.nan)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        # A pixel that no frame covers is an all-NaN slice. That is expected
        # at the edges of a mosaic, not an error.
        warnings.simplefilter("ignore", RuntimeWarning)
        centre = np.nanmedian(masked, axis=0)
        mad = np.nanmedian(np.abs(masked - centre), axis=0)
    scale = 1.4826 * _mad_correction(int(cube.shape[0])) * mad
    # Where the MAD collapses (few frames, identical values) fall back to the
    # global noise level so we do not reject everything.
    fallback = float(np.nanmedian(scale[np.isfinite(scale) & (scale > 0)])) if np.any(scale > 0) else 1.0
    scale = np.where(np.isfinite(scale) & (scale > 0), scale, fallback)
    return np.nan_to_num(centre, nan=0.0), scale


def _structure_image(centre: np.ndarray) -> np.ndarray:
    """Largest absolute first difference to a 4-neighbour, per pixel.

    drizzlepac calls this the "derivative image". It measures how steep the
    scene is locally, which is exactly how much frame-to-frame disagreement is
    legitimate: steep where a star is, flat where a cosmic ray lands.
    """
    a = np.asarray(centre, dtype=np.float32)
    out = np.zeros(a.shape, dtype=np.float32)
    for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
        diff = np.abs(a - np.roll(a, shift, axis=axis))
        out = np.maximum(out, diff)
    return out


def _median_inflation(n: int) -> float:
    r"""Scatter of ``x_i - median(x)`` relative to ``sigma``, for ``n`` samples.

    The per-pixel centre is itself estimated from the same ``n`` values, so the
    residual is wider than the noise: ``Var[median] ~ pi sigma**2 / (2n)``, and
    the residual scale is ``sigma * sqrt(1 + pi/(2n))``. Ignoring this turns a
    nominal 3-sigma clip into a 2.7-sigma clip for ``n = 8``.
    """
    if n < 2:
        return 1.0
    return float(np.sqrt(1.0 + np.pi / (2.0 * n)))


def sigma_clip_mask(
    cube: np.ndarray,
    good: np.ndarray | None = None,
    sigma_low: float = 5.0,
    sigma_high: float = 3.0,
    iterations: int = 3,
    min_keep: int = 2,
    scale: np.ndarray | None = None,
    structure_factor: float = 1.2,
) -> np.ndarray:
    r"""Iterative robust sigma clipping across the frame axis.

    Parameters
    ----------
    structure_factor
        Allowance for *legitimate* frame-to-frame differences, as a multiple
        of the local structure of the stack's median. This is the
        ``driz_cr``/DrizCR term of drizzlepac and it is not optional here:

        the frames in this corpus have **different PSFs**. At the core of a
        star, a 2-pixel-FWHM frame legitimately sits far above the median of a
        stack that also contains 6-pixel-FWHM frames. A plain sigma clip reads
        that as an outlier and throws away exactly the sharp frames the coadd
        most needs — measurably, it recovers only ~60% of a star's flux. The
        threshold therefore becomes

            |data - median| > sigma * noise + structure_factor * |grad median|

        where the second term scales with how fast the image is changing. A
        cosmic ray or a satellite trail sits where the median is *flat*, so it
        is still caught; a star core is where the median is steep, so it is
        not.
    scale
        Per-pixel noise sigma, shaped ``(N, H, W)`` or ``(H, W)``. **Strongly
        preferred** when a variance model exists: a MAD computed from a handful
        of frames has ~35% scatter of its own, so some pixels get an absurdly
        small scale and are clipped for no reason. On a pure-noise stack of 8
        frames the empirical route rejects ~2% of pixels against a true
        expectation of 0.14%, and those spurious rejections make the coadd
        *noisier*. When ``scale`` is ``None`` the MAD is used, with the
        Croux & Rousseeuw finite-sample correction applied.
    min_keep
        Never reduce a pixel below this many surviving frames: a pixel that
        only two frames cover must not be clipped to nothing because the two
        disagree.
    """
    data = np.asarray(cube, dtype=np.float32)
    keep = np.ones(data.shape, dtype=bool) if good is None else np.asarray(good, dtype=bool).copy()
    n = data.shape[0]
    inflation = _median_inflation(n)

    supplied_scale = None
    if scale is not None:
        s = np.asarray(scale, dtype=np.float32)
        if s.ndim == 2:
            s = np.broadcast_to(s, data.shape)
        if s.shape != data.shape:
            raise ValueError(f"scale shape {s.shape} does not match cube {data.shape}")
        supplied_scale = np.maximum(s, 1e-12) * inflation

    for _ in range(max(int(iterations), 1)):
        if supplied_scale is None:
            centre, est = _robust_centre_scale(data, keep)
            est = est * inflation
        else:
            centre, _ = _robust_centre_scale(data, keep)
            est = supplied_scale
        structure = _structure_image(centre) * float(structure_factor)
        residual = data - centre
        candidate = keep & (
            (residual <= sigma_high * est + structure)
            & (residual >= -(sigma_low * est + structure))
        )
        # Restore pixels where clipping would leave too few frames.
        counts = candidate.sum(axis=0)
        starved = counts < min(min_keep, n)
        candidate = np.where(starved[None, :, :], keep, candidate)
        if np.array_equal(candidate, keep):
            break
        keep = candidate
    return ~keep


def percentile_clip_mask(
    cube: np.ndarray,
    good: np.ndarray | None = None,
    low: float = 0.4,
    high: float = 0.3,
) -> np.ndarray:
    """Winsorising-style clip relative to the per-pixel median.

    Used when N is small (< 6), where the MAD is a poor scale estimate and
    sigma clipping either rejects nothing or rejects half the stack.
    """
    data = np.asarray(cube, dtype=np.float32)
    keep = np.ones(data.shape, dtype=bool) if good is None else np.asarray(good, dtype=bool).copy()
    masked = np.where(keep, data, np.nan)
    with np.errstate(invalid="ignore"):
        med = np.nanmedian(masked, axis=0)
    denom = np.where(np.abs(med) > 1e-12, np.abs(med), 1.0)
    rel = (data - med) / denom
    return ~(keep & (rel <= high) & (rel >= -low))


def trail_mask(
    cube: np.ndarray,
    outliers: np.ndarray,
    min_length: int = 12,
    min_elongation: float = 4.0,
    min_pixels: int = 10,
    dilation: int = 2,
) -> np.ndarray:
    """Flag satellite / aeroplane trails among the positive outliers.

    ``outliers`` is the boolean cube produced by a statistical pass. For each
    frame the positive outliers are labelled; a component is a trail when its
    second-moment elongation and its principal-axis extent both exceed the
    thresholds. The component is then dilated, since the faint ends of a trail
    do not individually reach the pixel threshold.
    """
    data = np.asarray(cube, dtype=np.float32)
    with np.errstate(invalid="ignore"):
        median = np.nanmedian(data, axis=0)
    positive = outliers & (data > median[None, :, :])

    out = np.zeros(data.shape, dtype=bool)
    structure = np.ones((3, 3), dtype=bool)
    for i in range(data.shape[0]):
        labels, n = ndimage.label(positive[i], structure=structure)
        if n == 0:
            continue
        objects = ndimage.find_objects(labels)
        frame_mask = np.zeros(data.shape[1:], dtype=bool)
        for lab, sl in enumerate(objects, start=1):
            if sl is None:
                continue
            sub = labels[sl] == lab
            npix = int(sub.sum())
            if npix < min_pixels:
                continue
            ys, xs = np.nonzero(sub)
            ys = ys.astype(np.float64)
            xs = xs.astype(np.float64)
            cov = np.cov(np.vstack([xs, ys])) if npix > 2 else np.zeros((2, 2))
            evals = np.linalg.eigvalsh(np.atleast_2d(cov))
            evals = np.clip(evals, 1e-9, None)
            elong = float(np.sqrt(evals[-1] / evals[0]))
            length = float(4.0 * np.sqrt(evals[-1]))
            if elong >= min_elongation and length >= min_length:
                frame_mask[sl][sub] = True
        if frame_mask.any() and dilation > 0:
            frame_mask = ndimage.binary_dilation(frame_mask, iterations=int(dilation))
        out[i] = frame_mask
    return out


def combined_rejection(
    cube: np.ndarray,
    good: np.ndarray | None = None,
    method: str = "sigma",
    sigma_low: float = 5.0,
    sigma_high: float = 3.0,
    iterations: int = 3,
    reject_trails: bool = True,
    min_keep: int = 2,
    scale: np.ndarray | None = None,
    structure_factor: float = 1.2,
    **trail_kwargs: float | int,
) -> RejectionResult:
    """Full rejection pass: statistics, then morphology.

    ``method``: ``sigma`` | ``percentile`` | ``none``. ``scale`` is the
    per-pixel noise sigma from the variance model; pass it whenever one
    exists (see :func:`sigma_clip_mask`).
    """
    data = np.asarray(cube, dtype=np.float32)
    base_good = np.ones(data.shape, dtype=bool) if good is None else np.asarray(good, dtype=bool)

    if method == "none":
        stat = ~base_good
    elif method == "percentile":
        stat = percentile_clip_mask(data, base_good)
    elif method == "sigma":
        stat = sigma_clip_mask(
            data, base_good, sigma_low=sigma_low, sigma_high=sigma_high,
            iterations=iterations, min_keep=min_keep, scale=scale,
            structure_factor=structure_factor,
        )  # fmt: skip
    else:
        raise ValueError(f"unknown rejection method {method!r}")

    n_stat = int((stat & base_good).sum())
    trails = np.zeros(data.shape, dtype=bool)
    if reject_trails and data.shape[0] >= 3:
        trails = trail_mask(data, stat & base_good, **trail_kwargs)  # type: ignore[arg-type]

    rejected = (stat | trails) & base_good
    # Guard again after trail dilation.
    survivors = (base_good & ~rejected).sum(axis=0)
    starved = survivors < min(min_keep, data.shape[0])
    rejected = np.where(starved[None, :, :], ~base_good, rejected)

    per_frame = rejected.reshape(data.shape[0], -1).mean(axis=1)
    return RejectionResult(
        rejected=rejected,
        n_sigma_clipped=n_stat,
        n_trail=int(trails.sum()),
        per_frame_fraction=per_frame,
    )
