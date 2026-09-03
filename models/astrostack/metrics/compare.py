"""Reference comparison metrics: PSNR, SSIM, and a flux-consistency score.

PSNR and SSIM are included because the burst-SR literature reports them and we
have to be comparable, **but section 4 of the research note is emphatic that
they are not sufficient**: STAR (NeurIPS 2025) introduced a flux-consistency
metric precisely because a network can win on PSNR while destroying
photometry. :func:`flux_consistency` is therefore reported alongside, and the
Tier B losses in :mod:`astrostack.sr.losses` optimise it directly.

SSIM is implemented here rather than pulled from scikit-image so that the base
install stays small and the windowing is explicit and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter

__all__ = ["ComparisonMetrics", "compare_images", "flux_consistency", "psnr", "ssim"]


def psnr(a: np.ndarray, b: np.ndarray, data_range: float | None = None) -> float:
    """Peak signal-to-noise ratio in dB, ``b`` treated as the reference."""
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    if x.shape != y.shape:
        raise ValueError(f"shape mismatch: {x.shape} vs {y.shape}")
    ok = np.isfinite(x) & np.isfinite(y)
    if not ok.any():
        return float("nan")
    mse = float(np.mean((x[ok] - y[ok]) ** 2))
    if mse <= 0:
        return float("inf")
    if data_range is None:
        finite = y[ok]
        data_range = float(np.max(finite) - np.min(finite)) or 1.0
    return float(10.0 * np.log10(data_range**2 / mse))


def ssim(
    a: np.ndarray,
    b: np.ndarray,
    window: int = 7,
    data_range: float | None = None,
    k1: float = 0.01,
    k2: float = 0.03,
) -> float:
    """Mean structural similarity (Wang et al. 2004) with a uniform window."""
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    if x.shape != y.shape:
        raise ValueError(f"shape mismatch: {x.shape} vs {y.shape}")
    x = np.nan_to_num(x)
    y = np.nan_to_num(y)
    if data_range is None:
        data_range = float(np.max(y) - np.min(y)) or 1.0
    c1 = (k1 * data_range) ** 2
    c2 = (k2 * data_range) ** 2

    win = max(int(window) | 1, 3)
    mu_x = uniform_filter(x, win)
    mu_y = uniform_filter(y, win)
    xx = uniform_filter(x * x, win)
    yy = uniform_filter(y * y, win)
    xy = uniform_filter(x * y, win)

    n = win * win
    cov_norm = n / (n - 1.0)
    var_x = cov_norm * (xx - mu_x * mu_x)
    var_y = cov_norm * (yy - mu_y * mu_y)
    cov_xy = cov_norm * (xy - mu_x * mu_y)

    num = (2 * mu_x * mu_y + c1) * (2 * cov_xy + c2)
    den = (mu_x**2 + mu_y**2 + c1) * (var_x + var_y + c2)
    smap = num / np.maximum(den, 1e-30)
    pad = win // 2
    if smap.shape[0] > 2 * pad and smap.shape[1] > 2 * pad:
        smap = smap[pad:-pad, pad:-pad]
    return float(np.mean(smap))


def flux_consistency(
    a: np.ndarray,
    b: np.ndarray,
    apertures: np.ndarray | None = None,
    radius: int = 5,
) -> dict[str, float]:
    """Photometric agreement between an output and its reference.

    Returns the total-flux ratio, plus — when source positions are supplied —
    the median and scatter of the per-source aperture flux ratio. The scatter
    is the number that catches a model which conserves flux *globally* while
    moving it between objects, which is the failure mode STAR was built to
    detect.
    """
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    total_a = float(np.nansum(x))
    total_b = float(np.nansum(y))
    out = {
        "total_flux_ratio": total_a / total_b if abs(total_b) > 1e-30 else float("nan"),
    }
    if apertures is None or len(apertures) == 0:
        return out

    pos = np.atleast_2d(np.asarray(apertures, dtype=np.float64))
    ratios = []
    r = int(radius)
    for yy, xx in pos:
        iy, ix = round(yy), round(xx)
        y0, y1 = max(iy - r, 0), min(iy + r + 1, x.shape[0])
        x0, x1 = max(ix - r, 0), min(ix + r + 1, x.shape[1])
        if y1 <= y0 or x1 <= x0:
            continue
        fa = float(np.nansum(x[y0:y1, x0:x1]))
        fb = float(np.nansum(y[y0:y1, x0:x1]))
        if abs(fb) > 1e-30:
            ratios.append(fa / fb)
    if ratios:
        arr = np.asarray(ratios)
        out["median_source_flux_ratio"] = float(np.median(arr))
        out["source_flux_scatter"] = float(np.std(arr))
        out["n_apertures"] = float(len(arr))
    return out


@dataclass(slots=True)
class ComparisonMetrics:
    """Full comparison of two images."""

    psnr_db: float
    ssim: float
    total_flux_ratio: float
    median_source_flux_ratio: float | None = None
    source_flux_scatter: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "psnr_db": self.psnr_db,
            "ssim": self.ssim,
            "total_flux_ratio": self.total_flux_ratio,
            "median_source_flux_ratio": self.median_source_flux_ratio,
            "source_flux_scatter": self.source_flux_scatter,
        }


def compare_images(
    image: np.ndarray,
    reference: np.ndarray,
    apertures: np.ndarray | None = None,
) -> ComparisonMetrics:
    """PSNR + SSIM + flux consistency in one call (used by the CLI)."""
    fc = flux_consistency(image, reference, apertures)
    return ComparisonMetrics(
        psnr_db=psnr(image, reference),
        ssim=ssim(image, reference),
        total_flux_ratio=fc["total_flux_ratio"],
        median_source_flux_ratio=fc.get("median_source_flux_ratio"),
        source_flux_scatter=fc.get("source_flux_scatter"),
    )
