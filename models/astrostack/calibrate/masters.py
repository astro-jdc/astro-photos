"""Bias / dark / flat calibration.

Most contributions to a public repository arrive uncalibrated, so every step
here is optional and declares what it does to the flux scale:

* bias and dark subtraction are **additive** corrections: they change the zero
  point of the frame but not its flux scale, so relative photometry survives;
* flat fielding is **multiplicative** by a unit-mean field: it preserves total
  flux only up to the normalisation, which is why we always divide by a flat
  normalised to its own median, and record the normalisation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrostack.io.frame import Frame

__all__ = ["MasterFrames", "apply_calibration", "combine_masters", "sigma_clipped_median"]


def sigma_clipped_median(
    stack: np.ndarray,
    sigma: float = 3.0,
    iterations: int = 3,
    axis: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Robust median with iterative sigma clipping.

    Returns ``(median, n_used)``. Deterministic: no random subsampling, MAD
    scaled by 1.4826 as the sigma estimate.
    """
    data = np.asarray(stack, dtype=np.float32)
    keep = np.isfinite(data)
    for _ in range(max(int(iterations), 0)):
        masked = np.where(keep, data, np.nan)
        with np.errstate(invalid="ignore"):
            med = np.nanmedian(masked, axis=axis, keepdims=True)
            mad = np.nanmedian(np.abs(masked - med), axis=axis, keepdims=True)
        scale = 1.4826 * mad
        scale = np.where(scale > 0, scale, np.inf)
        new_keep = keep & (np.abs(data - med) <= sigma * scale)
        # Never clip a pixel to nothing.
        empty = new_keep.sum(axis=axis, keepdims=True) == 0
        new_keep = np.where(empty, keep, new_keep)
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    masked = np.where(keep, data, np.nan)
    with np.errstate(invalid="ignore"):
        med = np.nanmedian(masked, axis=axis)
    return np.nan_to_num(med, nan=0.0).astype(np.float32), keep.sum(axis=axis).astype(np.int32)


@dataclass(slots=True)
class MasterFrames:
    """Master calibration frames on the sensor's own pixel grid."""

    bias: np.ndarray | None = None
    dark: np.ndarray | None = None
    dark_exposure_s: float | None = None
    dark_is_bias_subtracted: bool = True
    flat: np.ndarray | None = None
    flat_normalisation: float | None = None
    bad_pixels: np.ndarray | None = None

    def describe(self) -> dict[str, object]:
        return {
            "has_bias": self.bias is not None,
            "has_dark": self.dark is not None,
            "dark_exposure_s": self.dark_exposure_s,
            "has_flat": self.flat is not None,
            "flat_normalisation": self.flat_normalisation,
            "n_bad_pixels": int(self.bad_pixels.sum()) if self.bad_pixels is not None else 0,
        }


def combine_masters(
    bias_stack: np.ndarray | None = None,
    dark_stack: np.ndarray | None = None,
    flat_stack: np.ndarray | None = None,
    dark_exposure_s: float | None = None,
    sigma: float = 3.0,
    bad_pixel_sigma: float = 8.0,
) -> MasterFrames:
    """Build master frames from stacks shaped ``(N, H, W)``.

    The dark is bias-subtracted here so that it can later be scaled by the
    exposure ratio, which is only legitimate for the *thermal* component.
    """
    masters = MasterFrames(dark_exposure_s=dark_exposure_s)
    if bias_stack is not None:
        masters.bias, _ = sigma_clipped_median(bias_stack, sigma=sigma)
    if dark_stack is not None:
        dark, _ = sigma_clipped_median(dark_stack, sigma=sigma)
        if masters.bias is not None:
            dark = dark - masters.bias
        masters.dark = dark.astype(np.float32)
        hot_ref = masters.dark
        med = float(np.median(hot_ref))
        mad = float(np.median(np.abs(hot_ref - med))) * 1.4826
        if mad > 0:
            masters.bad_pixels = np.abs(hot_ref - med) > bad_pixel_sigma * mad
    if flat_stack is not None:
        flat, _ = sigma_clipped_median(flat_stack, sigma=sigma)
        if masters.bias is not None:
            flat = flat - masters.bias
        norm = float(np.median(flat[flat > 0])) if np.any(flat > 0) else 1.0
        masters.flat_normalisation = norm
        masters.flat = (flat / norm).astype(np.float32) if norm else flat.astype(np.float32)
    return masters


def apply_calibration(
    frame: Frame,
    masters: MasterFrames,
    flat_floor: float = 0.2,
) -> Frame:
    """Apply ``(raw - bias - dark_scaled) / flat`` in place-safe fashion.

    Pixels where the flat falls below ``flat_floor`` (heavy vignetting, dust
    motes, a dewed corner) are masked instead of being divided up, because
    dividing by a small flat amplifies noise without adding signal and then
    poisons the variance-based weights downstream.
    """
    data = frame.data.astype(np.float32, copy=True)
    var = frame.effective_variance().astype(np.float32, copy=True)
    mask = np.zeros(data.shape, dtype=bool) if frame.mask is None else frame.mask.copy()
    steps: list[str] = []

    if masters.bias is not None and masters.bias.shape == data.shape:
        data -= masters.bias
        steps.append("bias")
    if masters.dark is not None and masters.dark.shape == data.shape:
        scale = 1.0
        exp = frame.meta.exposure_seconds
        if exp and masters.dark_exposure_s:
            scale = float(exp) / float(masters.dark_exposure_s)
        data -= masters.dark * scale
        steps.append(f"dark x{scale:.3f}")
    if masters.bad_pixels is not None and masters.bad_pixels.shape == data.shape:
        mask |= masters.bad_pixels
        steps.append("bad-pixel map")
    if masters.flat is not None and masters.flat.shape == data.shape:
        flat = masters.flat
        low = flat < flat_floor
        safe = np.where(low, 1.0, flat)
        data = data / safe
        var = var / (safe * safe)
        mask |= low
        steps.append(f"flat (floor {flat_floor}, {int(low.sum())} px masked)")

    out = frame.copy_with(data, variance=var, mask=mask)
    if steps:
        out.note("calibrate.masters", " + ".join(steps), flux_preserving=True)
    else:
        out.note("calibrate.masters", "no calibration frames supplied", flux_preserving=True)
    return out
