"""Tier B — learned multi-frame super-resolution.

Importing this package does **not** import torch. The interfaces, the
registry, the WCS warping and the NumPy reference losses all work without it;
only instantiating a network needs the ``[torch]`` extra.
"""

from __future__ import annotations

from astrostack.sr.base import (
    SRInputs,
    SRResult,
    SuperResolver,
    build_resolver,
    get_resolver,
    register_resolver,
)
from astrostack.sr.losses import (
    combined_loss_numpy,
    flux_consistency_numpy,
    forward_model_fidelity_numpy,
    shape_moment_loss_numpy,
    shape_moments,
)
from astrostack.sr.uncertainty import (
    UncertaintyMaps,
    aggregate_samples,
    confidence_mask,
    prior_contribution,
)
from astrostack.sr.wcs_burst import WCSBurstSR, build_condition_channels, wcs_warp_stack

__all__ = [
    "SRInputs",
    "SRResult",
    "SuperResolver",
    "UncertaintyMaps",
    "WCSBurstSR",
    "aggregate_samples",
    "build_condition_channels",
    "build_resolver",
    "combined_loss_numpy",
    "confidence_mask",
    "flux_consistency_numpy",
    "forward_model_fidelity_numpy",
    "get_resolver",
    "prior_contribution",
    "register_resolver",
    "shape_moment_loss_numpy",
    "shape_moments",
    "wcs_warp_stack",
]
