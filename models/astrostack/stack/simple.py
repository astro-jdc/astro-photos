"""Classical combination: mean, median, sigma-clipped mean, winsorised mean.

**This is the honest baseline.** Rule 7 of the astro-ml brief says metrics are
always measured against a real baseline, not a straw man, so this module is
written to be *good*, not to lose: inverse-variance weighting, proper handling
of per-pixel masks, and an effective PSF that is actually computed rather than
assumed.

Everything here is flux preserving: a weighted mean of flux-calibrated frames
is itself in flux units. The median is flux preserving in expectation but
biased for skewed distributions, which is noted on the result.
"""

from __future__ import annotations

import warnings

import numpy as np

from astrostack.io.frame import Frame
from astrostack.stack.base import CoaddResult, as_cube, frame_weights
from astrostack.stack.reject import combined_rejection

__all__ = ["combine", "effective_psf_of_mean"]

_METHODS = ("mean", "median", "sigma-clip", "winsorized")


def effective_psf_of_mean(frames: list[Frame], weights: np.ndarray) -> np.ndarray | None:
    """PSF of a weighted mean: the weighted mean of the input PSFs.

    Exact for a linear combination of frames that share a flux scale, which is
    what makes the sigma-clipped mean a *fair* comparison target: we can
    matched-filter it with its own true PSF rather than pretending it has the
    sharpest input's PSF.
    """
    kernels = [f.psf for f in frames]
    if any(k is None for k in kernels):
        return None
    size = max(k.kernel.shape[0] for k in kernels if k is not None) | 1
    size = max(size, max(k.kernel.shape[1] for k in kernels if k is not None) | 1)
    acc = np.zeros((size, size), dtype=np.float64)
    for w, psf in zip(weights, kernels, strict=True):
        assert psf is not None
        k = np.asarray(psf.normalised(), dtype=np.float64)
        pad_y = (size - k.shape[0]) // 2
        pad_x = (size - k.shape[1]) // 2
        if pad_y < 0 or pad_x < 0:
            continue
        buf = np.zeros((size, size), dtype=np.float64)
        buf[pad_y : pad_y + k.shape[0], pad_x : pad_x + k.shape[1]] = k
        acc += float(w) * buf
    total = acc.sum()
    if total <= 0:
        return None
    return (acc / total).astype(np.float32)


def combine(
    frames: list[Frame],
    method: str = "sigma-clip",
    weighting: str = "inverse-variance",
    sigma_low: float = 5.0,
    sigma_high: float = 3.0,
    iterations: int = 3,
    reject_trails: bool = True,
    winsor_limit: float = 2.5,
) -> CoaddResult:
    """Combine co-registered frames with a classical estimator.

    Parameters
    ----------
    method
        ``mean`` | ``median`` | ``sigma-clip`` | ``winsorized``.
    weighting
        See :func:`astrostack.stack.base.frame_weights`.
    winsor_limit
        For ``winsorized``: values beyond this many robust sigma are *pulled
        in* to the limit rather than discarded, which keeps their weight while
        capping their leverage.
    """
    if method not in _METHODS:
        raise ValueError(f"unknown method {method!r}; known: {_METHODS}")

    data, var, good = as_cube(frames)
    n = data.shape[0]
    w_frame = frame_weights(frames, weighting)

    do_reject = method in ("sigma-clip", "winsorized")
    rejection = combined_rejection(
        data,
        good,
        method="sigma" if do_reject else "none",
        sigma_low=sigma_low,
        sigma_high=sigma_high,
        iterations=iterations,
        reject_trails=reject_trails and do_reject,
        # Clip against the variance model, not against a MAD of a handful of
        # frames: see astrostack.stack.reject.sigma_clip_mask.
        scale=np.sqrt(np.maximum(var, 1e-12)),
    )
    usable = good & ~rejection.rejected

    work = data.astype(np.float64)
    if method == "winsorized":
        masked = np.where(usable, work, np.nan)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            centre = np.nanmedian(masked, axis=0)
            mad = np.nanmedian(np.abs(masked - centre), axis=0)
        scale = np.where(mad > 0, 1.4826 * mad, np.inf)
        lo = centre - winsor_limit * scale
        hi = centre + winsor_limit * scale
        work = np.clip(work, lo[None], hi[None])

    weights = np.where(usable, w_frame[:, None, None], 0.0)
    wsum = weights.sum(axis=0)
    covered = wsum > 0

    if method == "median":
        masked = np.where(usable, work, np.nan)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            image = np.nanmedian(masked, axis=0)
        image = np.nan_to_num(image, nan=0.0)
        # Var(median) ~ (pi/2) Var(mean) for a Gaussian.
        eff_var = np.divide(
            (weights**2 * var).sum(axis=0), np.maximum(wsum, 1e-30) ** 2, where=covered,
            out=np.zeros_like(wsum),
        ) * (np.pi / 2.0)  # fmt: skip
    else:
        image = np.divide(
            (weights * work).sum(axis=0), np.maximum(wsum, 1e-30), where=covered,
            out=np.zeros_like(wsum),
        )  # fmt: skip
        eff_var = np.divide(
            (weights**2 * var).sum(axis=0), np.maximum(wsum, 1e-30) ** 2, where=covered,
            out=np.zeros_like(wsum),
        )  # fmt: skip

    image = np.where(covered, image, 0.0).astype(np.float32)
    uncertainty = np.sqrt(np.maximum(eff_var, 0.0)).astype(np.float32)

    psf = effective_psf_of_mean(frames, w_frame) if method != "median" else None

    notes = [
        f"{method} combination of {n} frames, weighting={weighting}",
        f"rejected {rejection.n_sigma_clipped} pixels statistically, "
        f"{rejection.n_trail} as satellite/aeroplane trails",
    ]
    if method == "median":
        notes.append(
            "median is flux-preserving in expectation only; it is biased for skewed "
            "pixel distributions and has ~1.25x the noise of the mean"
        )

    return CoaddResult(
        image=image,
        weight=wsum.astype(np.float32),
        uncertainty=uncertainty,
        psf=psf,
        wcs=frames[0].wcs,
        method=f"simple:{method}",
        n_frames=n,
        flux_preserving=True,
        frame_weights={f.frame_id: float(w) for f, w in zip(frames, w_frame, strict=True)},
        rejected_fraction={
            f.frame_id: float(v)
            for f, v in zip(frames, rejection.per_frame_fraction, strict=True)
        },
        metrics={
            "coverage_fraction": float(covered.mean()),
            "rejection": rejection.describe(),
        },
        notes=notes,
    )
