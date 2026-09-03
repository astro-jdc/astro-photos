"""Format dispatch: file on disk -> :class:`~astrostack.io.frame.Frame`.

Everything that leaves this module is ``float32`` and linear in flux (or
explicitly flagged as not being so). Nothing here invents metadata: values
that are not in the file or in the manifest stay ``None``, and downstream
stages must cope with ``None`` rather than with a plausible-looking default.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from astrostack.errors import UnsupportedFormatError
from astrostack.io.fits_io import read_fits
from astrostack.io.frame import Frame, FrameMetadata, FrameQuality
from astrostack.io.images import read_jpeg, read_tiff
from astrostack.io.raw import load_raw_planes
from astrostack.logging import get_logger

__all__ = [
    "RAW_EXTENSIONS",
    "compute_airmass",
    "detect_format",
    "load_frame",
    "pixel_scale_prior_arcsec",
    "poisson_variance",
]

log = get_logger(__name__)

RAW_EXTENSIONS = frozenset(
    {
        ".cr2", ".cr3", ".crw",           # Canon
        ".nef", ".nrw",                   # Nikon
        ".arw", ".srf", ".sr2",           # Sony
        ".raf",                           # Fujifilm
        ".orf",                           # Olympus
        ".rw2",                           # Panasonic
        ".pef",                           # Pentax
        ".dng",                           # Adobe / phones
        ".raw", ".rwl", ".iiq", ".3fr",
    }
)  # fmt: skip
FITS_EXTENSIONS = frozenset({".fits", ".fit", ".fts", ".fz"})
TIFF_EXTENSIONS = frozenset({".tif", ".tiff"})
LOSSY_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def detect_format(path: str | Path) -> str:
    """Return ``raw | fits | tiff | jpeg`` from the extension."""
    ext = Path(path).suffix.lower()
    if ext in RAW_EXTENSIONS:
        return "raw"
    if ext in FITS_EXTENSIONS:
        return "fits"
    if ext in TIFF_EXTENSIONS:
        return "tiff"
    if ext in LOSSY_EXTENSIONS:
        return "jpeg"
    raise UnsupportedFormatError(f"cannot infer a linearisable format from {path!r}")


def pixel_scale_prior_arcsec(
    focal_length_mm: float | None,
    pixel_pitch_um: float | None = None,
    sensor_width_mm: float | None = None,
    width_px: int | None = None,
    binning: int = 1,
) -> float | None:
    """Pixel scale from optics, used as the plate-solve scale prior.

    ``arcsec/px = 206.265 * pitch_um / focal_mm``. Section 6 of the research
    note: seeding astrometry.net with this speeds blind solving by orders of
    magnitude, so it is worth deriving even approximately.
    """
    if not focal_length_mm or focal_length_mm <= 0:
        return None
    pitch = pixel_pitch_um
    if pitch is None and sensor_width_mm and width_px:
        pitch = sensor_width_mm / width_px * 1000.0
    if not pitch or pitch <= 0:
        return None
    return float(206.264806 * pitch * binning / focal_length_mm)


def compute_airmass(
    ra_deg: float | None,
    dec_deg: float | None,
    when: datetime | None,
    latitude_deg: float | None,
    longitude_deg: float | None,
    elevation_m: float | None = 0.0,
) -> tuple[float | None, float | None, float | None]:
    """Return ``(airmass, altitude_deg, parallactic_angle_deg)``.

    Uses the Kasten & Young (1989) interpolative formula rather than plain
    ``sec z``, which diverges near the horizon. The parallactic angle is
    returned because differential chromatic refraction elongates the PSF
    *along it*, and a coaddition that ignores that mixes differently-oriented
    PSFs (section 8).
    """
    if None in (ra_deg, dec_deg, latitude_deg, longitude_deg) or when is None:
        return None, None, None
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    try:
        location = EarthLocation(
            lat=latitude_deg * u.deg,
            lon=longitude_deg * u.deg,
            height=(elevation_m or 0.0) * u.m,
        )
        target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
        frame = AltAz(obstime=Time(when), location=location)
        altaz = target.transform_to(frame)
        alt = float(altaz.alt.deg)
    except Exception as exc:  # noqa: BLE001 - ephemeris/IERS failures must not kill ingest
        log.warning("airmass_failed", error=str(exc))
        return None, None, None

    if alt <= 0.0:
        return None, alt, None
    z = 90.0 - alt
    airmass = 1.0 / (math.sin(math.radians(alt)) + 0.50572 * (6.07995 + alt) ** -1.6364)

    lat = math.radians(latitude_deg)
    dec = math.radians(dec_deg)
    # Hour angle from the observed az/alt, avoiding a second sidereal-time call.
    az = math.radians(float(altaz.az.deg))
    alt_r = math.radians(alt)
    num = math.sin(az) * math.cos(lat)
    den = math.cos(dec) * math.cos(alt_r)
    if abs(den) < 1e-12:
        parallactic = None
    else:
        parallactic = math.degrees(math.asin(max(-1.0, min(1.0, num / den * math.cos(alt_r)))))
    _ = z
    return float(airmass), alt, parallactic


def poisson_variance(
    data: np.ndarray,
    gain_e_per_adu: float | None,
    read_noise_e: float | None,
    background_rms: float | None = None,
) -> np.ndarray:
    """Per-pixel variance in the units of ``data``.

    With a known gain the model is the textbook one,
    ``var_adu = max(signal, 0) / g + (rn_e / g)**2``. Without a gain we fall
    back to a flat variance from the measured background RMS, which is the
    background-limited assumption that Zackay & Ofek work under anyway; the
    shot-noise term is then folded in only where the signal clearly dominates.
    """
    d = np.asarray(data, dtype=np.float32)
    if gain_e_per_adu and gain_e_per_adu > 0:
        rn = (read_noise_e or 0.0) / gain_e_per_adu
        return (np.maximum(d, 0.0) / gain_e_per_adu + rn * rn).astype(np.float32)
    if background_rms is None:
        med = float(np.nanmedian(d))
        mad = float(np.nanmedian(np.abs(d - med)))
        background_rms = 1.4826 * mad if mad > 0 else float(np.nanstd(d)) or 1.0
    var0 = float(background_rms) ** 2
    # Empirical scaling: treat the background level as the Poisson pivot.
    level = max(float(np.nanmedian(d)), 1e-6)
    excess = np.maximum(d - np.nanmedian(d), 0.0)
    return (var0 * (1.0 + excess / level)).astype(np.float32)


def load_frame(
    path: str | Path,
    meta: FrameMetadata | dict[str, Any] | None = None,
    channel: str = "G",
    fits_plane: int | None = None,
    demosaic_raw: bool = False,
    estimate_jpeg_gamma: bool = True,
) -> Frame:
    """Load any supported file into a linear :class:`Frame`."""
    path = Path(path)
    if isinstance(meta, dict):
        meta = FrameMetadata(**meta)
    if meta is None:
        meta = FrameMetadata(photo_id=path.stem)
    meta = meta.model_copy(update={"source_path": str(path)})

    fmt = detect_format(path)
    quality = FrameQuality()
    extra: dict[str, Any] = {}
    variance = None
    wcs = None
    binning = 1

    if fmt == "raw":
        planes = load_raw_planes(path, channels=(channel,), demosaic=demosaic_raw)
        p = planes[channel]
        data = p["data"]
        saturated = p["saturated"]
        binning = int(p["binning"])
        extra.update(
            {
                "cfa_offset": np.asarray(p["cfa_offset"]).tolist(),
                "white_level": p["white_level"],
                "black_level": p["black_level"],
                "camera_whitebalance": p["camera_whitebalance"],
                "cfa_interpolated": p["interpolated"],
            }
        )
        meta = meta.model_copy(update={"source_format": "raw", "channel": channel})
    elif fmt == "fits":
        r = read_fits(path, plane=fits_plane)
        data = r["data"]
        saturated = r["saturated"]
        variance = r["variance"]
        wcs = r["wcs"]
        extra["fits_header_keys"] = sorted(r["header"].keys())
        quality.is_plate_solved = wcs is not None
        if wcs is not None:
            quality.solver = "from-header"
        update: dict[str, Any] = {"source_format": "fits", "channel": meta.channel or channel}
        for key, value in (
            ("exposure_seconds", r["exposure_seconds"]),
            ("gain_e_per_adu", r["gain_e_per_adu"]),
            ("read_noise_e", r["read_noise_e"]),
            ("filter_name", r["filter_name"]),
        ):
            if getattr(meta, key) is None and value is not None:
                update[key] = value
        meta = meta.model_copy(update=update)
        if r["mask"] is not None:
            extra["input_mask"] = True
    elif fmt == "tiff":
        r = read_tiff(path, channel=channel)
        data, saturated = r["data"], r["saturated"]
        meta = meta.model_copy(
            update={
                "source_format": "tiff",
                "channel": channel,
                "bit_depth": r["bit_depth"],
                "photometrically_unreliable": r["photometrically_unreliable"],
                "unreliable_reason": r["unreliable_reason"],
            }
        )
        extra["tone_curve"] = r["tone_curve"]
    else:
        r = read_jpeg(path, channel=channel, estimate_gamma=estimate_jpeg_gamma)
        data, saturated = r["data"], r["saturated"]
        meta = meta.model_copy(
            update={
                "source_format": "jpeg",
                "channel": channel,
                "bit_depth": 8,
                "photometrically_unreliable": True,
                "unreliable_reason": r["unreliable_reason"],
            }
        )
        extra["tone_curve"] = r["tone_curve"]
        extra["exif"] = r["exif"]

    mask = None
    if fmt == "fits":
        mask = read_fits(path, plane=fits_plane)["mask"]

    frame = Frame(
        frame_id=meta.photo_id,
        data=data,
        meta=meta,
        quality=quality,
        variance=variance,
        saturated=saturated,
        mask=mask,
        wcs=wcs,
        extra=extra,
    )
    frame.quality.pixel_scale_arcsec = pixel_scale_prior_arcsec(
        meta.focal_length_mm,
        meta.pixel_pitch_um,
        meta.sensor_width_mm,
        data.shape[1] * binning,
        binning=binning,
    )
    if frame.variance is None:
        frame.variance = poisson_variance(data, meta.gain_e_per_adu, meta.read_noise_e)
    frame.note(
        "io.load",
        f"{fmt} -> linear float32 (channel {channel}, binning {binning})",
        flux_preserving=True,
    )
    if meta.photometrically_unreliable:
        frame.note("io.load", f"photometry degraded: {meta.unreliable_reason}", False)
    return frame
