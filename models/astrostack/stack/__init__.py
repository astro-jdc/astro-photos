"""Coaddition: the honest baseline, drizzle, and the optimal statistic."""

from __future__ import annotations

from astrostack.stack.base import CoaddResult, align_psf_kernels, as_cube, frame_weights
from astrostack.stack.drizzle import DrizzleAccumulator, drizzle
from astrostack.stack.optimal import optimal_coadd
from astrostack.stack.reject import (
    RejectionResult,
    combined_rejection,
    percentile_clip_mask,
    sigma_clip_mask,
    trail_mask,
)
from astrostack.stack.simple import combine, effective_psf_of_mean

__all__ = [
    "CoaddResult",
    "DrizzleAccumulator",
    "RejectionResult",
    "align_psf_kernels",
    "as_cube",
    "combine",
    "combined_rejection",
    "drizzle",
    "effective_psf_of_mean",
    "frame_weights",
    "optimal_coadd",
    "percentile_clip_mask",
    "sigma_clip_mask",
    "trail_mask",
]
