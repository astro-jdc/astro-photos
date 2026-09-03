"""Robust scale estimators shared across the package.

Kept in one place because getting the noise estimate wrong quietly poisons
everything downstream: the Zackay-Ofek weights are ``F_j / sigma_j**2``, the
rejection thresholds are in sigma, and every reported SNR divides by it.
"""

from __future__ import annotations

import numpy as np

__all__ = ["MAD_TO_SIGMA", "mad_std", "robust_sigma"]

#: 1 / Phi^-1(3/4). Converts a median absolute deviation into a Gaussian sigma.
MAD_TO_SIGMA = 1.4826


def mad_std(data: np.ndarray, mask: np.ndarray | None = None) -> float:
    """MAD-based sigma of ``data`` (``True`` in ``mask`` means *exclude*)."""
    a = np.asarray(data, dtype=np.float64)
    ok = np.isfinite(a)
    if mask is not None:
        ok &= ~np.asarray(mask, dtype=bool)
    vals = a[ok]
    if vals.size < 4:
        return float("nan")
    med = float(np.median(vals))
    return float(MAD_TO_SIGMA * np.median(np.abs(vals - med)))


def robust_sigma(
    data: np.ndarray,
    mask: np.ndarray | None = None,
    upper_percentile: float = 90.0,
) -> float:
    """Background sigma, with the brightest pixels excluded.

    A plain MAD is already resistant to a few outliers, but a star field with
    broad PSF wings puts *some* flux in a large fraction of pixels, which
    inflates it by tens of percent. Trimming the top decile before the MAD
    removes that bias, and the result matches the true background sigma of a
    synthetic field to about a percent.
    """
    a = np.asarray(data, dtype=np.float64)
    ok = np.isfinite(a)
    if mask is not None:
        ok &= ~np.asarray(mask, dtype=bool)
    vals = a[ok]
    if vals.size < 8:
        return float("nan")
    cut = float(np.percentile(vals, upper_percentile))
    trimmed = vals[vals <= cut]
    if trimmed.size < 8:
        trimmed = vals
    med = float(np.median(trimmed))
    sigma = float(MAD_TO_SIGMA * np.median(np.abs(trimmed - med)))
    # Trimming the upper tail of a symmetric distribution biases the MAD low
    # by a known factor; undo it so the estimate stays unbiased on pure noise.
    from scipy.stats import norm

    q = upper_percentile / 100.0
    z = float(norm.ppf(q))
    # Expected MAD of a standard normal truncated above at z, relative to 1.
    correction = _truncated_mad_correction(z)
    return sigma / correction if correction > 0 else sigma


def _truncated_mad_correction(z: float) -> float:
    """MAD of a standard normal truncated above at ``z``, in units of sigma.

    Computed once by bisection on the truncated CDF rather than tabulated, so
    the value stays correct if ``upper_percentile`` changes.
    """
    from scipy.stats import norm

    mass = norm.cdf(z)
    if mass <= 0.5:
        return 1.0
    # Median of the truncated distribution.
    med = float(norm.ppf(0.5 * mass))
    # MAD: the m such that P(|X - med| <= m | X <= z) = 0.5
    lo, hi = 1e-6, 10.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        prob = (norm.cdf(min(med + mid, z)) - norm.cdf(med - mid)) / mass
        if prob < 0.5:
            lo = mid
        else:
            hi = mid
    return float(0.5 * (lo + hi) * MAD_TO_SIGMA)
