"""Measurement: quality, reference comparison, and the injection audit."""

from __future__ import annotations

from astrostack.metrics.compare import (
    ComparisonMetrics,
    compare_images,
    flux_consistency,
    psnr,
    ssim,
)
from astrostack.metrics.injection import (
    InjectedSource,
    InjectionReport,
    false_positive_rate,
    inject_into_frames,
    injection_experiment,
    measure_matched_flux,
    plan_injection_grid,
)
from astrostack.metrics.quality import (
    MeasuredQuality,
    depth_curve,
    effective_pixel_scale,
    flux_ratio,
    matched_filter_snr,
    measure_fwhm,
    noise_equivalent_fwhm,
    snr_gain_db,
)

__all__ = [
    "ComparisonMetrics",
    "InjectedSource",
    "InjectionReport",
    "MeasuredQuality",
    "compare_images",
    "depth_curve",
    "effective_pixel_scale",
    "false_positive_rate",
    "flux_consistency",
    "flux_ratio",
    "inject_into_frames",
    "injection_experiment",
    "matched_filter_snr",
    "measure_fwhm",
    "measure_matched_flux",
    "noise_equivalent_fwhm",
    "plan_injection_grid",
    "psnr",
    "snr_gain_db",
    "ssim",
]
