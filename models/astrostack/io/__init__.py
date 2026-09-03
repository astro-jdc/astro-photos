"""Ingest, linearisation and delivery."""

from __future__ import annotations

from astrostack.io.frame import Frame, FrameMetadata, FrameQuality, PSFModel
from astrostack.io.loaders import (
    compute_airmass,
    detect_format,
    load_frame,
    pixel_scale_prior_arcsec,
    poisson_variance,
)
from astrostack.io.manifest import InputSpec, Manifest, load_manifest
from astrostack.io.tone import invert_tone_curve, srgb_to_linear
from astrostack.io.writers import (
    asinh_stretch,
    checksum_arrays,
    write_preview_png,
    write_result_fits,
)

__all__ = [
    "Frame",
    "FrameMetadata",
    "FrameQuality",
    "InputSpec",
    "Manifest",
    "PSFModel",
    "asinh_stretch",
    "checksum_arrays",
    "compute_airmass",
    "detect_format",
    "invert_tone_curve",
    "load_frame",
    "load_manifest",
    "pixel_scale_prior_arcsec",
    "poisson_variance",
    "srgb_to_linear",
    "write_preview_png",
    "write_result_fits",
]
