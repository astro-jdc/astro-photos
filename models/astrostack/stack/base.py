"""Shared coaddition types.

Every stacker in this package returns a :class:`CoaddResult`. It carries not
just the image but the three things that make the output auditable:

* a **weight map** (how much data went into each pixel),
* an **uncertainty map** (1-sigma, in the same units as the image),
* the **effective PSF** of the coadd.

Plus ``flux_preserving``, because rule 2 of the astro-ml brief requires every
operation to declare what it does to photometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from astropy.wcs import WCS

from astrostack.io.frame import Frame

__all__ = ["CoaddResult", "align_psf_kernels", "as_cube", "frame_weights"]


@dataclass(slots=True)
class CoaddResult:
    """One combined image plus everything needed to judge it."""

    image: np.ndarray
    weight: np.ndarray
    method: str
    n_frames: int
    uncertainty: np.ndarray | None = None
    psf: np.ndarray | None = None
    wcs: WCS | None = None
    flux_preserving: bool = True
    frame_weights: dict[str, float] = field(default_factory=dict)
    rejected_fraction: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return self.image.shape  # type: ignore[return-value]

    def summary(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "n_frames": self.n_frames,
            "shape": list(self.image.shape),
            "flux_preserving": self.flux_preserving,
            "frame_weights": {k: round(float(v), 8) for k, v in sorted(self.frame_weights.items())},
            "rejected_fraction": {
                k: round(float(v), 8) for k, v in sorted(self.rejected_fraction.items())
            },
            "metrics": self.metrics,
            "notes": list(self.notes),
        }


def as_cube(frames: list[Frame]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stack co-registered frames into ``(data, variance, good)`` cubes.

    Input order is the caller's responsibility, but it is checked: frames must
    already be sorted by ``frame_id`` because float summation is not
    associative and the reproducibility contract is bit-for-bit.
    """
    if not frames:
        raise ValueError("no frames to combine")
    ids = [f.frame_id for f in frames]
    if ids != sorted(ids):
        raise ValueError(
            "frames must be sorted by frame_id before combination "
            "(float addition is not associative; unsorted input breaks reproducibility)"
        )
    shape = frames[0].shape
    for f in frames:
        if f.shape != shape:
            raise ValueError(
                f"frame {f.frame_id!r} has shape {f.shape}, expected {shape}; "
                "register onto a common grid before stacking"
            )
    data = np.stack([f.data.astype(np.float32) for f in frames])
    var = np.stack([f.effective_variance().astype(np.float32) for f in frames])
    good = np.stack([f.good for f in frames])
    return data, var, good


def frame_weights(frames: list[Frame], scheme: str = "inverse-variance") -> np.ndarray:
    """Scalar per-frame weights.

    ``inverse-variance``
        ``1 / sigma_j**2``. The classic choice.
    ``zackay-ofek``
        ``F_j / sigma_j**2``, transparency over variance — the *scalar* part of
        the optimal weighting (the PSF matched filter is the other part and
        lives in :mod:`astrostack.stack.optimal`).
    ``exposure``
        Proportional to exposure time; what a naive stacker does.
    ``uniform``
        All equal. The straw man, kept only so it can be beaten honestly.
    """
    w = np.ones(len(frames), dtype=np.float64)
    for i, f in enumerate(frames):
        sigma = f.quality.noise_sigma or f.quality.background_rms
        if sigma is None or sigma <= 0:
            sigma = float(np.sqrt(np.nanmedian(f.effective_variance()))) or 1.0
        transparency = float(f.quality.transparency or 1.0)
        if scheme == "inverse-variance":
            w[i] = 1.0 / (sigma * sigma)
        elif scheme == "zackay-ofek":
            w[i] = transparency / (sigma * sigma)
        elif scheme == "exposure":
            w[i] = float(f.meta.exposure_seconds or 1.0)
        elif scheme == "uniform":
            w[i] = 1.0
        else:
            raise ValueError(f"unknown weighting scheme {scheme!r}")
        if f.meta.photometrically_unreliable and scheme == "zackay-ofek":
            # A JPEG contributor has no trustworthy F_j. Keep it in the stack
            # for SNR, but never let it drive the flux scale.
            w[i] = 1.0 / (sigma * sigma)
    total = float(w.sum())
    if total <= 0 or not np.isfinite(total):
        return np.full(len(frames), 1.0 / len(frames))
    return w / total


def align_psf_kernels(frames: list[Frame], shape: tuple[int, int]) -> np.ndarray:
    """Zero-pad every frame's PSF into ``shape``, centred at the array origin.

    Placing the PSF centre at pixel (0, 0) (via ``ifftshift``) is what makes
    the FFT convolution shift-free, which matters: a half-pixel systematic
    shift between the matched filter and the data costs real detection
    significance.
    """
    out = np.zeros((len(frames), *shape), dtype=np.float64)
    for i, f in enumerate(frames):
        if f.psf is None:
            raise ValueError(
                f"frame {f.frame_id!r} has no measured PSF; run "
                "astrostack.align.stars.characterise_frame first"
            )
        k = np.asarray(f.psf.normalised(), dtype=np.float64)
        kh, kw = k.shape
        if kh > shape[0] or kw > shape[1]:
            raise ValueError(f"PSF {k.shape} larger than image {shape}")
        pad = np.zeros(shape, dtype=np.float64)
        y0 = (shape[0] - kh) // 2
        x0 = (shape[1] - kw) // 2
        pad[y0 : y0 + kh, x0 : x0 + kw] = k
        # Move the kernel centre to (0, 0).
        pad = np.roll(pad, (-(y0 + kh // 2), -(x0 + kw // 2)), axis=(0, 1))
        out[i] = pad
    return out
