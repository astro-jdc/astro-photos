"""Tone-curve inversion for non-linear contributions (JPEG, 8-bit TIFF/PNG).

Section 8 of the research note is blunt about why this matters: JPEG is 8-bit,
gamma-encoded, chroma-subsampled and lossy; *it is not linear in flux*, so
stacking JPEGs without inverting the tone curve destroys photometric validity
and biases the coadd.

Two things are inverted here, and they are epistemically very different:

1. **The sRGB EOTF.** Standardised, exactly known, always applied.
2. **A residual power law** left over from the camera's picture style or the
   contributor's stretch. This one has to be *estimated*, and the estimator
   below is a photon-transfer argument:

   For a background-limited sky, the linear signal ``x`` is Poisson so
   ``Var[x] = k*x``. A monotone curve ``y = x**(1/g)`` propagates as
   ``Var[y] = (1/g**2) * x**(2/g - 2) * Var[x]``. Substituting ``x = y**g``:

       Var[y] = (k / g**2) * mean(y) ** (2 - g)

   so a straight-line fit of ``log Var[y]`` against ``log mean(y)`` over many
   background patches has slope ``s = 2 - g``, giving ``g = 2 - s``.

   The per-patch scale is measured from first differences between adjacent
   pixels (DER_SNR, Stoehr et al. 2008) so that the light-pollution gradient
   does not get counted as noise, and the fit is accepted or rejected on the
   **standard error of the slope**, not on R^2 — a frame with gamma near 2 has
   a slope near zero and therefore an R^2 near zero however well determined it
   is.

   This works when the frame is genuinely photon-noise dominated and the
   residual curve is roughly a power law. It fails on denoised, heavily
   sharpened, or clipped images — which is exactly why *every* frame that goes
   through this module is flagged ``photometrically_unreliable=True``
   regardless of how well the fit went.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["ToneCurveEstimate", "estimate_residual_gamma", "invert_tone_curve", "srgb_to_linear"]


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """Invert the sRGB electro-optical transfer function (IEC 61966-2-1).

    ``x`` must already be scaled to [0, 1]. Exact, not estimated.
    """
    x = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    lo = x / 12.92
    hi = ((x + 0.055) / 1.055) ** 2.4
    return np.where(x <= 0.04045, lo, hi).astype(np.float32)


@dataclass(slots=True)
class ToneCurveEstimate:
    """Result of the photon-transfer gamma fit."""

    gamma: float
    n_patches: int
    slope: float
    r_squared: float
    converged: bool
    reason: str = ""
    slope_error: float = float("inf")

    @property
    def gamma_error(self) -> float:
        """1-sigma uncertainty on the recovered exponent."""
        return self.slope_error

    def as_dict(self) -> dict[str, float | int | bool | str]:
        return {
            "gamma": self.gamma,
            "gamma_error": self.slope_error,
            "n_patches": self.n_patches,
            "slope": self.slope,
            "r_squared": self.r_squared,
            "converged": self.converged,
            "reason": self.reason,
        }


def _patch_stats(image: np.ndarray, patch: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-patch (mean, variance), robust to stars *and* to sky gradients.

    The scale comes from the median absolute difference between horizontally
    adjacent pixels (the DER_SNR estimator of Stoehr et al. 2008), not from a
    plain MAD about the patch median. That matters here: light pollution puts
    a smooth ramp across the frame, and a plain MAD would measure the ramp
    rather than the noise — inflating the variance most where the sky is
    brightest, which is precisely the direction that biases the fitted
    exponent. A first difference cancels any locally linear component exactly.

    For Gaussian noise the difference of two neighbours has sigma
    ``sqrt(2) sigma``, so ``sigma = 1.4826 * median|diff| / sqrt(2)``.
    """
    h, w = image.shape
    ny, nx = h // patch, w // patch
    if ny < 2 or nx < 2:
        return np.empty(0), np.empty(0)
    trimmed = image[: ny * patch, : nx * patch]
    blocks = trimmed.reshape(ny, patch, nx, patch).transpose(0, 2, 1, 3)
    means = blocks.reshape(ny * nx, -1).mean(axis=1)

    diffs = np.abs(np.diff(blocks, axis=3)).reshape(ny * nx, -1)
    sigma = 1.4826 * np.median(diffs, axis=1) / np.sqrt(2.0)
    return means.astype(np.float64), (sigma**2).astype(np.float64)


def estimate_residual_gamma(
    image: np.ndarray,
    patch: int = 24,
    gamma_bounds: tuple[float, float] = (0.5, 6.0),
    min_patches: int = 24,
    max_slope_error: float = 0.3,
) -> ToneCurveEstimate:
    """Estimate the residual power-law exponent ``g`` of a non-linear frame.

    ``image`` is expected in [0, 1] after sRGB inversion. Returns
    ``gamma == 1.0`` (i.e. "assume already linear") whenever the fit is not
    trustworthy, together with the reason.
    """
    img = np.asarray(image, dtype=np.float64)
    finite = np.isfinite(img)
    if not finite.all():
        img = np.where(finite, img, np.nan)

    means, var = _patch_stats(np.nan_to_num(img, nan=0.0), patch)
    if means.size == 0:
        return ToneCurveEstimate(1.0, 0, 0.0, 0.0, False, "image too small for patch statistics")

    # Keep background patches only: no clipping, non-degenerate noise.
    keep = (means > 1e-4) & (means < 0.85) & (var > 1e-12)
    if keep.sum() < min_patches:
        return ToneCurveEstimate(
            1.0, int(keep.sum()), 0.0, 0.0, False, "too few usable background patches"
        )
    # Drop the brightest decile: those patches are object, not sky.
    m, v = means[keep], var[keep]
    cut = np.quantile(m, 0.9)
    sel = m <= cut
    if sel.sum() >= min_patches:
        m, v = m[sel], v[sel]

    x = np.log(m)
    y = np.log(v)
    if np.ptp(x) < 0.15:
        return ToneCurveEstimate(
            1.0, int(m.size), 0.0, 0.0, False, "background too flat to constrain the curve"
        )

    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # The acceptance test is the *standard error of the slope*, not R^2. The
    # two are not interchangeable here: the fitted slope is 2 - gamma, so a
    # frame with gamma near 2 has a slope near zero, no variance for the fit to
    # explain, and an R^2 near zero no matter how well determined the slope is.
    # Gating on R^2 would therefore reject exactly the most common case (a
    # standard 2.2 display gamma) while accepting noisier fits elsewhere.
    n = int(m.size)
    sxx = float(np.sum((x - x.mean()) ** 2))
    if n > 2 and sxx > 0:
        slope_error = float(np.sqrt((ss_res / (n - 2)) / sxx))
    else:  # pragma: no cover - guarded by min_patches above
        slope_error = float("inf")

    gamma = float(2.0 - slope)
    if not np.isfinite(gamma) or not (gamma_bounds[0] <= gamma <= gamma_bounds[1]):
        return ToneCurveEstimate(
            1.0, n, float(slope), r2, False,
            f"gamma {gamma:.3f} outside plausible bounds", slope_error,
        )  # fmt: skip
    if slope_error > max_slope_error:
        return ToneCurveEstimate(
            1.0, n, float(slope), r2, False,
            f"gamma poorly constrained (+/-{slope_error:.2f})", slope_error,
        )  # fmt: skip
    return ToneCurveEstimate(gamma, n, float(slope), r2, True, "ok", slope_error)


def invert_tone_curve(
    image: np.ndarray,
    assume_srgb: bool = True,
    estimate_gamma: bool = True,
    forced_gamma: float | None = None,
) -> tuple[np.ndarray, ToneCurveEstimate]:
    """Return an approximately linear image plus a description of what was undone.

    The output is **not** photometrically calibrated; it is merely closer to
    linear than the input. Callers must set
    ``FrameMetadata.photometrically_unreliable = True``.
    """
    x = np.asarray(image, dtype=np.float32)
    lin = srgb_to_linear(x) if assume_srgb else np.clip(x, 0.0, 1.0)

    if forced_gamma is not None:
        est = ToneCurveEstimate(float(forced_gamma), 0, 0.0, 0.0, True, "gamma supplied by caller")
    elif estimate_gamma:
        est = estimate_residual_gamma(lin)
    else:
        est = ToneCurveEstimate(1.0, 0, 0.0, 0.0, False, "residual gamma estimation disabled")

    if abs(est.gamma - 1.0) > 1e-3:
        lin = np.power(np.clip(lin, 0.0, 1.0), est.gamma, dtype=np.float32)
    return lin.astype(np.float32), est
