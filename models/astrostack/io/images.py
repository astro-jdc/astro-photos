"""TIFF / JPEG / PNG ingest.

TIFF at 16 or 32 bits from Siril, PixInsight or DSS is normally already
linear, so it is read as-is. Anything 8-bit — and every JPEG — goes through
:mod:`astrostack.io.tone` and is flagged photometrically unreliable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import ExifTags, Image

from astrostack.errors import UnsupportedFormatError
from astrostack.io.tone import invert_tone_curve

__all__ = ["read_exif", "read_jpeg", "read_tiff"]

_CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2, "L": None}


def _select_channel(arr: np.ndarray, channel: str) -> np.ndarray:
    """Reduce an (H, W[, C]) array to one 2-D plane."""
    if arr.ndim == 2:
        return arr
    if arr.ndim != 3:
        raise UnsupportedFormatError(f"expected 2-D or 3-D image, got shape {arr.shape}")
    if arr.shape[2] == 1:
        return arr[:, :, 0]
    idx = _CHANNEL_INDEX.get(channel.upper(), 1)
    if idx is None:
        # Luminance in *linear* light: equal-energy weights, not Rec.709.
        # Rec.709 weights are defined on gamma-encoded values and would bias
        # the flux scale of a linear astronomical frame.
        return arr[:, :, :3].mean(axis=2)
    return arr[:, :, min(idx, arr.shape[2] - 1)]


def read_tiff(path: str | Path, channel: str = "G") -> dict[str, Any]:
    """Read a TIFF. 16/32-bit is assumed linear; 8-bit is not."""
    arr = np.asarray(tifffile.imread(str(path)))
    if arr.dtype == np.uint8:
        raw = _select_channel(arr.astype(np.float32) / 255.0, channel)
        linear, est = invert_tone_curve(raw)
        return {
            "data": linear.astype(np.float32),
            "saturated": raw >= (254.0 / 255.0),
            "photometrically_unreliable": True,
            "unreliable_reason": "8-bit TIFF: tone curve inverted, quantised to 256 levels",
            "tone_curve": est.as_dict(),
            "bit_depth": 8,
        }

    plane = _select_channel(arr, channel).astype(np.float32)
    if arr.dtype == np.uint16:
        sat_level = 65535.0
    elif arr.dtype == np.int16:
        sat_level = 32767.0
    else:
        sat_level = float(np.nanmax(plane)) if plane.size else 1.0
    return {
        "data": plane,
        "saturated": plane >= sat_level * (1.0 - 1e-6),
        "photometrically_unreliable": False,
        "unreliable_reason": None,
        "tone_curve": None,
        "bit_depth": 16 if arr.dtype.itemsize == 2 else 32,
    }


def read_jpeg(
    path: str | Path,
    channel: str = "G",
    estimate_gamma: bool = True,
    forced_gamma: float | None = None,
) -> dict[str, Any]:
    """Read a JPEG/PNG and invert its tone curve as best we can.

    The result is *always* marked photometrically unreliable. That flag is not
    cosmetic: :mod:`astrostack.stack.optimal` refuses to derive a Zackay-Ofek
    transparency ``F_j`` from such a frame and falls back to a variance-only
    weight, and the pipeline records the degradation in ``provenance.json``.
    """
    with Image.open(path) as img:
        exif = read_exif(img)
        rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0

    encoded = _select_channel(rgb, channel)
    linear, est = invert_tone_curve(
        encoded, assume_srgb=True, estimate_gamma=estimate_gamma, forced_gamma=forced_gamma
    )
    reason = "8-bit gamma-encoded lossy source"
    if est.converged:
        reason += (
            f"; residual gamma {est.gamma:.2f} +/- {est.gamma_error:.2f} recovered by photon transfer"
        )
    else:
        reason += f"; residual gamma NOT recovered ({est.reason}), only sRGB EOTF undone"
    return {
        "data": linear.astype(np.float32),
        "saturated": encoded >= (254.0 / 255.0),
        "photometrically_unreliable": True,
        "unreliable_reason": reason,
        "tone_curve": est.as_dict(),
        "bit_depth": 8,
        "exif": exif,
    }


def read_exif(img: Image.Image) -> dict[str, Any]:
    """Flatten PIL EXIF into a plain, JSON-safe dict."""
    out: dict[str, Any] = {}
    try:
        raw = img.getexif()
    except Exception:  # noqa: BLE001
        return out
    if not raw:
        return out
    for tag, value in raw.items():
        name = ExifTags.TAGS.get(tag, str(tag))
        out[name] = _jsonable(value)
    for ifd_name, ifd_tag in (("Exif", 0x8769), ("GPSInfo", 0x8825)):
        try:
            ifd = raw.get_ifd(ifd_tag)
        except Exception:  # noqa: BLE001
            continue
        table = ExifTags.GPSTAGS if ifd_name == "GPSInfo" else ExifTags.TAGS
        for tag, value in (ifd or {}).items():
            out[table.get(tag, f"{ifd_name}:{tag}")] = _jsonable(value)
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[:256]
    if isinstance(value, tuple | list):
        return [_jsonable(v) for v in value]
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)[:256]
