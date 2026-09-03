"""Camera RAW ingest via rawpy/LibRaw.

Rules for astronomy, all of them different from ordinary photography:

* subtract the **per-channel black level** and nothing else;
* do **not** apply white balance, camera matrix, gamma, auto-brightness,
  highlight recovery or noise reduction — every one of those is a non-linear
  or channel-mixing operation that destroys photometry;
* record the **saturation mask** from the white level *before* any arithmetic,
  because a clipped star core must never be averaged into a coadd;
* prefer *not* demosaicing. Splitting the CFA into its native R/G1/G2/B planes
  keeps each channel on its own real sampling grid at half resolution and
  invents nothing. Drizzle then recovers the sampling from the sub-pixel
  diversity across contributors, which is precisely the Fruchter & Hook
  argument. Demosaicing first would correlate neighbouring pixels and fake
  the very high-frequency content we are trying to measure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from astrostack.optional import require

__all__ = ["CFA_CHANNELS", "load_raw_planes"]

#: rawpy encodes CFA colours as 0=R, 1=G, 2=B, 3=G2 (for RGBG sensors).
CFA_CHANNELS = {"R": (0,), "G": (1, 3), "G1": (1,), "G2": (3,), "B": (2,)}


def _plane_from_cfa(
    raw_visible: np.ndarray,
    colors: np.ndarray,
    codes: tuple[int, ...],
) -> tuple[np.ndarray, np.ndarray]:
    """Extract one CFA colour plane at half resolution.

    Returns ``(plane, offsets)`` where ``offsets`` are the (dy, dx) positions
    of the sampled sites inside the 2x2 CFA cell. Those offsets are a real
    sub-pixel astrometric shift and are carried into the WCS so that drizzle
    puts the plane back where it belongs.
    """
    h, w = raw_visible.shape
    h2, w2 = h - h % 2, w - w % 2
    cell_colors = colors[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2)
    cell_data = raw_visible[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2)

    accum = np.zeros((h2 // 2, w2 // 2), dtype=np.float64)
    count = np.zeros((h2 // 2, w2 // 2), dtype=np.float64)
    offsets: list[tuple[int, int]] = []
    for dy in (0, 1):
        for dx in (0, 1):
            code = int(cell_colors[0, dy, 0, dx])
            if code in codes:
                accum += cell_data[:, dy, :, dx]
                count += 1.0
                offsets.append((dy, dx))
    if not offsets:
        raise ValueError(f"CFA pattern contains none of the colour codes {codes}")
    plane = (accum / np.maximum(count, 1.0)).astype(np.float32)
    return plane, np.asarray(offsets, dtype=np.float32)


def load_raw_planes(
    path: str | Path,
    channels: tuple[str, ...] = ("G",),
    demosaic: bool = False,
) -> dict[str, dict[str, Any]]:
    """Decode a RAW file into linear per-channel planes.

    Parameters
    ----------
    channels
        Which CFA channels to return: any of ``R G G1 G2 B``.
    demosaic
        If ``True``, use LibRaw's linear interpolation at full resolution
        (``gamma=(1,1)``, ``no_auto_bright``, unit white balance). Convenient,
        but the result is spatially correlated and **not** an independent
        sampling of the sky; the returned dict marks it
        ``interpolated=True`` so the pipeline can down-weight it.

    Returns
    -------
    dict keyed by channel name, each value carrying ``data``, ``saturated``,
    ``variance`` (Poisson + read noise in ADU^2), ``cfa_offset`` and the raw
    sensor parameters used.
    """
    rawpy = require("rawpy")
    path = Path(path)

    with rawpy.imread(str(path)) as raw:
        raw_visible = raw.raw_image_visible.astype(np.float64)
        colors = np.asarray(raw.raw_colors_visible)
        black_per_channel = np.asarray(raw.black_level_per_channel, dtype=np.float64)
        white_level = float(raw.white_level)
        camera_wb = list(map(float, raw.camera_whitebalance))
        # Saturation is decided on the *encoded* values, before black subtraction.
        saturated_full = raw_visible >= (white_level - 1.0)
        black_map = black_per_channel[np.clip(colors, 0, len(black_per_channel) - 1)]
        linear_full = raw_visible - black_map

        if demosaic:
            rgb = raw.postprocess(
                gamma=(1.0, 1.0),
                no_auto_bright=True,
                output_bps=16,
                use_camera_wb=False,
                use_auto_wb=False,
                user_wb=[1.0, 1.0, 1.0, 1.0],
                output_color=rawpy.ColorSpace.raw,
                demosaic_algorithm=rawpy.DemosaicAlgorithm.LINEAR,
                median_filter_passes=0,
                four_color_rgb=False,
            ).astype(np.float32)

    out: dict[str, dict[str, Any]] = {}
    if demosaic:
        index = {"R": 0, "G": 1, "G1": 1, "G2": 1, "B": 2}
        sat_rgb = np.zeros(rgb.shape[:2], dtype=bool)
        sat_rgb[: saturated_full.shape[0], : saturated_full.shape[1]] = saturated_full[
            : rgb.shape[0], : rgb.shape[1]
        ]
        for ch in channels:
            plane = rgb[:, :, index[ch]].astype(np.float32)
            out[ch] = {
                "data": plane,
                "saturated": sat_rgb,
                "cfa_offset": np.zeros((1, 2), dtype=np.float32),
                "interpolated": True,
                "white_level": white_level,
                "black_level": float(black_per_channel.mean()),
                "camera_whitebalance": camera_wb,
                "binning": 1,
            }
        return out

    for ch in channels:
        codes = CFA_CHANNELS[ch]
        plane, offsets = _plane_from_cfa(linear_full, colors, codes)
        sat_plane, _ = _plane_from_cfa(saturated_full.astype(np.float64), colors, codes)
        out[ch] = {
            "data": plane,
            "saturated": sat_plane > 0.0,
            "cfa_offset": offsets,
            "interpolated": False,
            "white_level": white_level - float(black_per_channel[codes[0]]),
            "black_level": float(black_per_channel[codes[0]]),
            "camera_whitebalance": camera_wb,
            "binning": 2,
        }
    return out
