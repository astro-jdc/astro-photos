"""Instrumental signature removal: masters, cosmic rays, sky background."""

from __future__ import annotations

from astrostack.calibrate.background import (
    BackgroundModel,
    estimate_background,
    subtract_background,
)
from astrostack.calibrate.cosmicray import CosmicRayResult, lacosmic
from astrostack.calibrate.masters import (
    MasterFrames,
    apply_calibration,
    combine_masters,
    sigma_clipped_median,
)

__all__ = [
    "BackgroundModel",
    "CosmicRayResult",
    "MasterFrames",
    "apply_calibration",
    "combine_masters",
    "estimate_background",
    "lacosmic",
    "sigma_clipped_median",
    "subtract_background",
]
