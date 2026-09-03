"""FITS ingest and output.

FITS is the only lossless, linear, WCS-carrying format in the corpus, so it is
both the preferred input and the mandated output (rule 6 of the astro-ml
brief: *linear 32-bit FITS with weight and uncertainty maps*).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from astrostack.errors import UnsupportedFormatError

__all__ = ["read_fits", "wcs_from_header"]

_VARIANCE_EXTNAMES = ("VARIANCE", "VAR", "ERR", "ERROR", "SIGMA", "UNCERT")
_MASK_EXTNAMES = ("MASK", "DQ", "BADPIX", "FLAGS")


def wcs_from_header(header: fits.Header) -> WCS | None:
    """Build a WCS if the header actually carries a celestial solution."""
    try:
        wcs = WCS(header, relax=True)
    except Exception:  # noqa: BLE001 - malformed headers are common in the wild
        return None
    if not wcs.has_celestial:
        return None
    return wcs.celestial


def _first_image_hdu(hdul: fits.HDUList) -> tuple[int, fits.Header, np.ndarray]:
    for i, hdu in enumerate(hdul):
        data = getattr(hdu, "data", None)
        if data is not None and np.ndim(data) >= 2:
            return i, hdu.header, np.asarray(data)
    raise UnsupportedFormatError("FITS file contains no image HDU")


def read_fits(path: str | Path, plane: int | None = None) -> dict[str, Any]:
    """Read a FITS image into linear float32 plus its ancillary maps.

    Handles 3-D cubes by selecting ``plane`` (default 0) and records which
    plane was taken, since a colour FITS from an OSC camera has three
    physically distinct passbands that must not be averaged.
    """
    path = Path(path)
    with fits.open(path, memmap=False) as hdul:
        idx, header, data = _first_image_hdu(hdul)
        selected_plane = None
        if data.ndim == 3:
            selected_plane = 0 if plane is None else int(plane)
            data = data[selected_plane]
        elif data.ndim > 3:
            raise UnsupportedFormatError(f"FITS data has {data.ndim} dimensions; expected 2 or 3")

        variance = None
        mask = None
        for hdu in hdul:
            name = str(hdu.header.get("EXTNAME", "")).upper()
            if hdu.data is None or np.ndim(hdu.data) != 2:
                continue
            arr = np.asarray(hdu.data)
            if arr.shape != data.shape:
                continue
            if variance is None and name in _VARIANCE_EXTNAMES:
                variance = arr.astype(np.float32)
                if name in ("ERR", "ERROR", "SIGMA", "UNCERT"):
                    variance = variance**2
            elif mask is None and name in _MASK_EXTNAMES:
                mask = arr.astype(bool)

        wcs = wcs_from_header(header)
        saturate = header.get("SATURATE")
        saturated = None
        if saturate is not None:
            saturated = np.asarray(data, dtype=np.float64) >= float(saturate)

        return {
            "data": np.asarray(data, dtype=np.float32),
            "header": dict(header),
            "hdu_index": idx,
            "plane": selected_plane,
            "wcs": wcs,
            "variance": variance,
            "mask": mask,
            "saturated": saturated,
            "exposure_seconds": _header_float(header, "EXPTIME", "EXPOSURE"),
            "gain_e_per_adu": _header_float(header, "GAIN", "EGAIN"),
            "read_noise_e": _header_float(header, "RDNOISE", "READNOIS"),
            "filter_name": header.get("FILTER"),
            "date_obs": header.get("DATE-OBS"),
        }


def _header_float(header: fits.Header, *keys: str) -> float | None:
    for key in keys:
        if key in header:
            try:
                return float(header[key])
            except (TypeError, ValueError):
                continue
    return None
