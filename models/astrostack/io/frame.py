"""The :class:`Frame` — one linear exposure plus everything needed to model it.

The metadata schema mirrors section 8 of
``docs/research/multi-image-astro-reconstruction.md`` and the ``photos`` table
of ``docs/data-model.md``. It is split in two:

``FrameMetadata``
    Declared / EXIF facts. Supplied by the caller (the backend already has
    them in Postgres). Never invented by the pipeline.

``FrameQuality``
    Derived by the pipeline: measured PSF, sky level, zero point, variance,
    airmass. Section 8 is explicit that these must be *measured*, not trusted.

Every array is ``float32`` and **linear in flux**. If a loader could not
guarantee that, it sets ``photometrically_unreliable=True`` and the weighting
stages down-rank the frame.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from astropy.wcs import WCS
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Frame", "FrameMetadata", "FrameQuality", "PSFModel"]


class FrameMetadata(BaseModel):
    """Declared / EXIF metadata. One row of ``photos``, essentially."""

    model_config = ConfigDict(extra="allow")

    photo_id: str = Field(description="Stable id; also the deterministic sort key.")
    source_path: str | None = None
    owner_display_name: str | None = None
    attribution_name: str | None = None

    # --- licence gates enforced locally (authority is the backend) ---
    license: str | None = None
    allow_ai_training: bool = True
    allow_derivatives_in_stacks: bool = True

    # --- time ---
    captured_at_utc: datetime | None = None
    utc_offset_minutes: int | None = None
    time_source: str | None = None  # exif | gps | user | inferred
    exposure_seconds: float | None = None

    # --- place ---
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    elevation_m: float | None = None
    bortle: int | None = None

    # --- optics / sensor ---
    camera_make: str | None = None
    camera_model: str | None = None
    sensor_width_mm: float | None = None
    sensor_height_mm: float | None = None
    pixel_pitch_um: float | None = None
    bit_depth: int | None = None
    lens_model: str | None = None
    telescope_model: str | None = None
    focal_length_mm: float | None = None
    focal_ratio: float | None = None
    aperture_mm: float | None = None
    iso: int | None = None
    gain_e_per_adu: float | None = None
    read_noise_e: float | None = None
    is_tracked: bool | None = None
    mount_model: str | None = None
    filter_name: str | None = None
    channel: str | None = Field(
        default=None,
        description="Which colour/narrowband plane this Frame carries: R/G/B/L/Ha/OIII/SII/mono.",
    )
    is_stacked: bool = False
    sub_frames: int | None = None

    # --- provenance of the pixels themselves ---
    source_format: str | None = None  # raw | fits | tiff | jpeg | synthetic
    photometrically_unreliable: bool = False
    unreliable_reason: str | None = None

    @property
    def aperture_or_estimate_mm(self) -> float | None:
        """Aperture in mm, derived from f/# when not given."""
        if self.aperture_mm:
            return self.aperture_mm
        if self.focal_length_mm and self.focal_ratio:
            return self.focal_length_mm / self.focal_ratio
        return None

    def diffraction_limit_arcsec(self, wavelength_nm: float = 550.0) -> float | None:
        """Rayleigh limit 1.22 lambda / D, in arcsec.

        This is the hard wall of section 5: no amount of stacking resolves
        past it. Used by :mod:`astrostack.enhance` to cap deconvolution.
        """
        d_mm = self.aperture_or_estimate_mm
        if not d_mm:
            return None
        return float(1.22 * (wavelength_nm * 1e-9) / (d_mm * 1e-3) * 206264.806)


class FrameQuality(BaseModel):
    """Pipeline-derived quality description. All of it is measured."""

    model_config = ConfigDict(extra="allow")

    pixel_scale_arcsec: float | None = None
    orientation_deg: float | None = None
    parity: int | None = None
    is_plate_solved: bool = False
    solver: str | None = None

    fwhm_pixels: float | None = None
    fwhm_arcsec: float | None = None
    eccentricity: float | None = None
    star_count: int | None = None

    background_adu: float | None = None
    background_rms: float | None = None
    sky_gradient_amplitude: float | None = None

    zero_point: float | None = None
    color_term: float | None = None
    extinction_coeff: float | None = None
    transparency: float = Field(
        default=1.0,
        description="Relative throughput F_j of Zackay & Ofek. 1.0 = photometric.",
    )
    noise_sigma: float | None = Field(
        default=None, description="Background sigma_j of Zackay & Ofek."
    )

    airmass: float | None = None
    parallactic_angle_deg: float | None = None
    moon_illumination: float | None = None
    moon_separation_deg: float | None = None

    trailing_metric: float | None = None
    quality_score: float | None = None


@dataclass(slots=True)
class PSFModel:
    """A measured PSF: a normalised kernel plus its field-variation summary.

    ``kernel`` is the field-averaged ePSF (sum == 1). ``field_fwhm`` /
    ``field_ecc`` / ``field_theta`` are coarse maps over the frame, because a
    single global number is wrong for alt-az field rotation and for
    differential chromatic refraction (section 8).
    """

    kernel: np.ndarray
    pixel_scale_arcsec: float | None = None
    fwhm_pixels: float | None = None
    eccentricity: float | None = None
    theta_deg: float | None = None
    field_grid_shape: tuple[int, int] | None = None
    field_fwhm: np.ndarray | None = None
    field_ecc: np.ndarray | None = None
    field_theta: np.ndarray | None = None
    n_stars: int = 0

    def normalised(self) -> np.ndarray:
        """Kernel rescaled to unit sum (flux-preserving convolution)."""
        s = float(np.sum(self.kernel))
        if not np.isfinite(s) or s <= 0:
            raise ValueError("PSF kernel has non-positive sum")
        return (self.kernel / s).astype(np.float32)

    @property
    def is_field_varying(self) -> bool:
        if self.field_fwhm is None:
            return False
        finite = self.field_fwhm[np.isfinite(self.field_fwhm)]
        if finite.size < 4:
            return False
        return bool(finite.std() / max(finite.mean(), 1e-9) > 0.15)


@dataclass(slots=True)
class Frame:
    """One linear exposure on its own pixel grid."""

    frame_id: str
    data: np.ndarray
    meta: FrameMetadata
    quality: FrameQuality = field(default_factory=FrameQuality)
    variance: np.ndarray | None = None
    saturated: np.ndarray | None = None
    mask: np.ndarray | None = None
    wcs: WCS | None = None
    psf: PSFModel | None = None
    background: np.ndarray | None = None
    history: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.data = np.ascontiguousarray(self.data, dtype=np.float32)
        if self.data.ndim != 2:
            raise ValueError(
                f"Frame {self.frame_id!r}: data must be 2-D (one channel per Frame), "
                f"got shape {self.data.shape}. Split colour planes before constructing."
            )
        for name in ("variance", "background"):
            arr = getattr(self, name)
            if arr is not None:
                setattr(self, name, np.ascontiguousarray(arr, dtype=np.float32))
        for name in ("saturated", "mask"):
            arr = getattr(self, name)
            if arr is not None:
                setattr(self, name, np.ascontiguousarray(arr, dtype=bool))

    # -- geometry ---------------------------------------------------------
    @property
    def shape(self) -> tuple[int, int]:
        return self.data.shape  # type: ignore[return-value]

    @property
    def good(self) -> np.ndarray:
        """Boolean array of usable pixels."""
        ok = np.isfinite(self.data)
        if self.mask is not None:
            ok &= ~self.mask
        if self.saturated is not None:
            ok &= ~self.saturated
        return ok

    def effective_variance(self, floor: float = 1e-12) -> np.ndarray:
        """Variance map, synthesised from the background RMS when absent."""
        if self.variance is not None:
            return np.maximum(self.variance, floor)
        rms = self.quality.background_rms
        if rms is None:
            rms = float(np.nanstd(self.data)) or 1.0
        return np.full(self.data.shape, max(rms**2, floor), dtype=np.float32)

    # -- bookkeeping ------------------------------------------------------
    def note(self, stage: str, message: str, flux_preserving: bool) -> None:
        """Record a processing step.

        Rule 2 of the astro-ml brief: *every* operation declares whether it
        conserves flux. The flag lands in ``provenance.json``.
        """
        tag = "flux-preserving" if flux_preserving else "NOT-flux-preserving"
        self.history.append(f"{stage}: {message} [{tag}]")

    def checksum(self) -> str:
        """sha256 of the pixel data. Backs the reproducibility test."""
        h = hashlib.sha256()
        h.update(np.ascontiguousarray(self.data, dtype=np.float32).tobytes())
        return h.hexdigest()

    def summary(self) -> dict[str, Any]:
        """JSON-serialisable description for ``provenance.json``."""
        return {
            "frame_id": self.frame_id,
            "photo_id": self.meta.photo_id,
            "shape": list(self.data.shape),
            "source_format": self.meta.source_format,
            "photometrically_unreliable": self.meta.photometrically_unreliable,
            "license": self.meta.license,
            "attribution_name": self.meta.attribution_name or self.meta.owner_display_name,
            "has_wcs": self.wcs is not None,
            "quality": self.quality.model_dump(exclude_none=True),
            "data_sha256": self.checksum(),
            "history": list(self.history),
        }

    def copy_with(self, data: np.ndarray, **kwargs: Any) -> Frame:
        """Shallow copy carrying new pixel data."""
        base = {
            "frame_id": self.frame_id,
            "data": data,
            "meta": self.meta,
            "quality": self.quality,
            "variance": self.variance,
            "saturated": self.saturated,
            "mask": self.mask,
            "wcs": self.wcs,
            "psf": self.psf,
            "background": self.background,
            "history": list(self.history),
            "extra": dict(self.extra),
        }
        base.update(kwargs)
        return Frame(**base)
