"""Deliverables: linear FITS, weight/uncertainty maps, PSF, PNG preview.

The FITS writer is deliberately timestamp-free. ``astropy`` does not stamp
``DATE`` unless asked, and we do not ask: the reproducibility contract says
that the same inputs and params must give the same *bytes*, and a header
timestamp would silently break it. Wall-clock information belongs in
``provenance.json``, which is excluded from the output checksum.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

__all__ = [
    "ascii_safe",
    "asinh_stretch",
    "checksum_arrays",
    "write_preview_png",
    "write_result_fits",
]

#: Characters that turn up in our own prose and that FITS cannot hold.
_ASCII_SUBSTITUTIONS = {
    "\u2014": "--",   # em dash
    "\u2013": "-",    # en dash
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u00d7": "x",    # multiplication sign
    "\u00b1": "+/-",
    "\u03c3": "sigma",
    "\u03bb": "lambda",
    "\u00b5": "u",
}


def ascii_safe(text: object, limit: int = 72) -> str:
    """Coerce to printable 7-bit ASCII for a FITS card.

    FITS headers are ASCII by standard, and astropy enforces it. Our stage
    notes are written for humans and contain em dashes, sigmas and lambdas, so
    they are transliterated here rather than being kept out of the header —
    the provenance of a coadd belongs *in* the file, not only beside it.
    """
    s = str(text)
    for bad, good in _ASCII_SUBSTITUTIONS.items():
        s = s.replace(bad, good)
    s = s.encode("ascii", "replace").decode("ascii")
    s = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in s)
    return s[:limit]


def checksum_arrays(**arrays: np.ndarray | None) -> str:
    """sha256 over named arrays, order-independent and dtype-explicit.

    This is the number the reproducibility test compares.
    """
    h = hashlib.sha256()
    for name in sorted(arrays):
        arr = arrays[name]
        h.update(name.encode("utf-8"))
        if arr is None:
            h.update(b"<none>")
            continue
        a = np.ascontiguousarray(arr)
        h.update(str(a.dtype.str).encode("utf-8"))
        h.update(str(a.shape).encode("utf-8"))
        h.update(a.tobytes())
    return h.hexdigest()


def write_result_fits(
    path: str | Path,
    image: np.ndarray,
    weight: np.ndarray | None = None,
    uncertainty: np.ndarray | None = None,
    psf: np.ndarray | None = None,
    wcs: WCS | None = None,
    header_cards: dict[str, Any] | None = None,
    history: list[str] | None = None,
) -> str:
    """Write the standard astro-photos result file.

    Extensions, in order: primary ``SCI`` (float32 linear), ``WEIGHT``,
    ``UNCERT`` (1-sigma, same units as ``SCI``), ``PSF`` (unit-sum effective
    PSF of the coadd). Returns the sha256 of the array payloads.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    hdr = fits.Header()
    if wcs is not None:
        hdr.update(wcs.to_header(relax=True))
    hdr["EXTNAME"] = "SCI"
    hdr["BUNIT"] = ("adu", "linear flux units of the coadd")
    for key, value in (header_cards or {}).items():
        card_key = str(key)[:8].upper()
        if isinstance(value, tuple):
            hdr[card_key] = value
        elif isinstance(value, np.floating | np.integer):
            hdr[card_key] = value.item()
        elif isinstance(value, str):
            hdr[card_key] = ascii_safe(value, 68)
        elif isinstance(value, float | int | bool) or value is None:
            hdr[card_key] = value
        else:
            hdr[card_key] = ascii_safe(value, 68)
    for line in history or []:
        hdr.add_history(ascii_safe(line))

    hdus: list[fits.hdu.base._BaseHDU] = [
        fits.PrimaryHDU(data=np.asarray(image, dtype=np.float32), header=hdr)
    ]
    for name, arr, unit in (
        ("WEIGHT", weight, "relative"),
        ("UNCERT", uncertainty, "adu"),
        ("PSF", psf, "normalised"),
    ):
        if arr is None:
            continue
        ext_header = fits.Header()
        ext_header["EXTNAME"] = name
        ext_header["BUNIT"] = unit
        if name != "PSF" and wcs is not None:
            ext_header.update(wcs.to_header(relax=True))
        hdus.append(fits.ImageHDU(data=np.asarray(arr, dtype=np.float32), header=ext_header))

    fits.HDUList(hdus).writeto(path, overwrite=True, output_verify="silentfix")
    return checksum_arrays(image=image, weight=weight, uncertainty=uncertainty, psf=psf)


def asinh_stretch(
    image: np.ndarray,
    black_percentile: float = 25.0,
    white_percentile: float = 99.85,
    softening: float = 0.02,
) -> np.ndarray:
    """Deterministic asinh display stretch, in [0, 1].

    Purely cosmetic: the PNG preview is for humans, the FITS is for science.
    Percentile-based so the same data always yields the same picture.
    """
    a = np.asarray(image, dtype=np.float64)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return np.zeros(a.shape, dtype=np.float32)
    lo = float(np.percentile(finite, black_percentile))
    hi = float(np.percentile(finite, white_percentile))
    if hi <= lo:
        hi = lo + 1e-6
    x = np.clip((a - lo) / (hi - lo), 0.0, 1.0)
    soft = max(float(softening), 1e-6)
    y = np.arcsinh(x / soft) / np.arcsinh(1.0 / soft)
    return np.nan_to_num(y, nan=0.0).astype(np.float32)


def write_preview_png(path: str | Path, image: np.ndarray, **stretch_kwargs: Any) -> None:
    """Write an 8-bit stretched preview."""
    from PIL import Image

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    y = asinh_stretch(image, **stretch_kwargs)
    Image.fromarray((np.clip(y, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8), mode="L").save(path)
