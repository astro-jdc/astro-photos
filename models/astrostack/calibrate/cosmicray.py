"""L.A.Cosmic cosmic-ray rejection.

Implementation of van Dokkum (2001), PASP 113, 1420, *Cosmic-Ray Rejection by
Laplacian Edge Detection*. The algorithm exploits the one property that
separates a cosmic ray from a star: a cosmic ray is sharper than the PSF, so
it has a much larger Laplacian relative to its own flux.

Steps, per iteration:

1. Subsample the image 2x (block replication), convolve with the Laplacian
   kernel ``[[0,-1,0],[-1,4,-1],[0,-1,0]]``, keep the positive part and
   resample back down. This gives ``L+``.
2. Build the noise model ``N = sqrt(median5(I)/gain + rn**2/gain**2)`` from a
   5x5-median version of the image, so that the noise is not itself inflated
   by the cosmic ray.
3. ``S = L+ / (2 N)``; subtract a 5x5 median of ``S`` to remove the smooth
   contribution of genuine structure, giving ``S'``.
4. Build the fine-structure image ``F = median3(I) - median7(median3(I))``,
   which is large for real, PSF-sized objects and small for a cosmic ray.
5. Flag pixels with ``S' > sigclip`` **and** ``L+/F > objlim``.

Cosmic rays are *masked*, never interpolated over by default: an interpolated
pixel is invented data, and in a stack with N frames there is almost always a
real measurement of that pixel in another frame.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import convolve, median_filter

__all__ = ["CosmicRayResult", "lacosmic"]

_LAPLACIAN = np.array([[0.0, -1.0, 0.0], [-1.0, 4.0, -1.0], [0.0, -1.0, 0.0]], dtype=np.float64)
_GROW = np.ones((3, 3), dtype=np.float64)


def _subsample2(a: np.ndarray) -> np.ndarray:
    return np.repeat(np.repeat(a, 2, axis=0), 2, axis=1)


def _rebin2(a: np.ndarray) -> np.ndarray:
    h, w = a.shape
    return a.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))


@dataclass(slots=True)
class CosmicRayResult:
    """Mask of affected pixels plus the diagnostics needed to tune the run."""

    mask: np.ndarray
    n_flagged: int
    iterations: int
    cleaned: np.ndarray | None = None

    def fraction(self) -> float:
        return float(self.n_flagged) / float(self.mask.size)


def lacosmic(
    image: np.ndarray,
    gain_e_per_adu: float = 1.0,
    read_noise_e: float = 5.0,
    sigclip: float = 4.5,
    sigfrac: float = 0.3,
    objlim: float = 5.0,
    max_iter: int = 4,
    background: np.ndarray | float | None = None,
    clean: bool = False,
) -> CosmicRayResult:
    """Detect cosmic rays. Returns a boolean mask (``True`` = affected).

    Parameters
    ----------
    sigfrac
        Neighbours of a flagged pixel are flagged too when they exceed
        ``sigfrac * sigclip``; this catches the wings of a long track without
        lowering the primary threshold.
    clean
        If ``True``, also return a version with flagged pixels replaced by the
        5x5 median of their unflagged neighbours. Off by default: in a stack
        the honest fix is to drop the pixel and let another frame supply it.
    """
    work = np.array(image, dtype=np.float64, copy=True)
    if background is not None:
        work = work - background
    finite = np.isfinite(work)
    work = np.where(finite, work, 0.0)

    gain = float(gain_e_per_adu) if gain_e_per_adu and gain_e_per_adu > 0 else 1.0
    rn = float(read_noise_e or 0.0)

    mask = np.zeros(work.shape, dtype=bool)
    current = work.copy()
    used_iter = 0

    for it in range(max(int(max_iter), 1)):
        used_iter = it + 1

        # 1. Laplacian on a 2x-subsampled grid.
        sub = _subsample2(current)
        lap = convolve(sub, _LAPLACIAN, mode="nearest")
        lap = np.clip(lap, 0.0, None)
        l_plus = _rebin2(lap)

        # 2. Noise model from a median-filtered image (cosmic-ray free).
        med5 = median_filter(current, size=5, mode="nearest")
        noise = np.sqrt(np.clip(med5, 0.0, None) / gain + (rn / gain) ** 2)
        noise = np.maximum(noise, 1e-6)

        # 3. Significance, with the smooth component removed.
        s = l_plus / (2.0 * noise)
        s_prime = s - median_filter(s, size=5, mode="nearest")

        # 4. Fine-structure image.
        med3 = median_filter(current, size=3, mode="nearest")
        fine = med3 - median_filter(med3, size=7, mode="nearest")
        fine = np.maximum(fine, 0.01)

        # 5. Combined criterion.
        candidates = (s_prime > sigclip) & (l_plus / fine > objlim)

        # Grow into neighbours above the relaxed threshold.
        grown = convolve(candidates.astype(np.float64), _GROW, mode="constant", cval=0.0) > 0
        neighbours = grown & (s_prime > sigclip * sigfrac) & (l_plus / fine > objlim)
        new = (candidates | neighbours) & ~mask
        if not new.any():
            break
        mask |= new
        # Replace flagged pixels before the next pass so they stop dominating
        # the Laplacian; this is internal scratch, not the returned image.
        current = np.where(mask, med5, current)

    mask &= finite
    cleaned = None
    if clean:
        med5 = median_filter(np.where(mask, np.nan, work), size=5, mode="nearest")
        cleaned = np.where(mask, np.nan_to_num(med5, nan=float(np.nanmedian(work))), work)
        cleaned = cleaned.astype(np.float32)

    return CosmicRayResult(
        mask=mask, n_flagged=int(mask.sum()), iterations=used_iter, cleaned=cleaned
    )
