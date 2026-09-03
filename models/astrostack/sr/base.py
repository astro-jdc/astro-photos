"""Tier B interface: ``SuperResolver`` and the architecture registry.

Product framing, straight from section 9: *the learned stage should be an
optional, clearly-labelled enhancement layer over the Tier-A scientific
coadd — never a silent replacement for it.* The interface enforces that:

* every resolver declares its ``architecture`` name and ``scale``;
* :meth:`SuperResolver.enhance` returns an :class:`SRResult` that **must**
  carry a per-pixel uncertainty map;
* :class:`SRResult.label` is the string the frontend has to display next to
  the image. There is no code path that produces a Tier B image without one.

The registry lets ``configs/burst-sr-v1.yaml`` name an architecture as a
string, and lets the ``models`` table's ``architecture`` column round-trip.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from astrostack.io.frame import Frame
from astrostack.stack.base import CoaddResult

__all__ = ["SRInputs", "SRResult", "SuperResolver", "build_resolver", "get_resolver", "register_resolver"]

_REGISTRY: dict[str, Callable[..., SuperResolver]] = {}


def register_resolver(name: str) -> Callable[[type[SuperResolver]], type[SuperResolver]]:
    """Class decorator registering an architecture under ``name``."""

    def wrap(cls: type[SuperResolver]) -> type[SuperResolver]:
        key = name.lower()
        if key in _REGISTRY:
            raise ValueError(f"resolver {name!r} already registered")
        _REGISTRY[key] = cls
        cls.architecture = key
        return cls

    return wrap


def get_resolver(name: str) -> Callable[..., SuperResolver]:
    key = str(name).lower()
    if key not in _REGISTRY:
        raise KeyError(f"unknown super-resolution architecture {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[key]


def build_resolver(name: str, **kwargs: Any) -> SuperResolver:
    """Factory used by the pipeline config."""
    return get_resolver(name)(**kwargs)


@dataclass(slots=True)
class SRInputs:
    """Everything the network is allowed to see.

    The *physical conditioning* of section 9, Tier B step 3: *feed the network
    each frame's PSF kernel, noise sigma map, zero point, background and
    airmass as explicit side inputs.* These are not optional extras — an
    unconditioned network cannot know that frame 7 was shot through cirrus at
    airmass 2.4, and will average it in as if it were good data.
    """

    frames: list[Frame]
    reference_coadd: CoaddResult | None = None
    output_grid: Any = None
    psf_kernels: list[np.ndarray] = field(default_factory=list)
    sigma_maps: list[np.ndarray] = field(default_factory=list)
    zero_points: list[float] = field(default_factory=list)
    airmasses: list[float] = field(default_factory=list)
    backgrounds: list[float] = field(default_factory=list)

    @classmethod
    def from_frames(
        cls,
        frames: list[Frame],
        reference_coadd: CoaddResult | None = None,
        output_grid: Any = None,
    ) -> SRInputs:
        """Collect the side inputs from already-characterised frames."""
        ordered = sorted(frames, key=lambda f: f.frame_id)
        psfs, sigmas, zps, airmass, backgrounds = [], [], [], [], []
        for fr in ordered:
            if fr.psf is None:
                raise ValueError(
                    f"{fr.frame_id}: Tier B conditioning needs a measured PSF. "
                    "Run astrostack.align.stars.characterise_frame first."
                )
            psfs.append(fr.psf.normalised())
            sigmas.append(np.sqrt(fr.effective_variance()).astype(np.float32))
            zps.append(float(fr.quality.zero_point or 0.0))
            airmass.append(float(fr.quality.airmass or 1.0))
            backgrounds.append(float(fr.quality.background_adu or 0.0))
        return cls(
            frames=ordered,
            reference_coadd=reference_coadd,
            output_grid=output_grid,
            psf_kernels=psfs,
            sigma_maps=sigmas,
            zero_points=zps,
            airmasses=airmass,
            backgrounds=backgrounds,
        )

    def __len__(self) -> int:
        return len(self.frames)


@dataclass(slots=True)
class SRResult:
    """A learned enhancement, with its mandatory audit attached."""

    image: np.ndarray
    uncertainty: np.ndarray
    architecture: str
    scale: float
    model_id: str | None = None
    baseline: CoaddResult | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    prior_contribution: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The visible label the UI is required to show (hard rule 2)."""
        return (
            f"AI-enhanced ({self.architecture}, x{self.scale:g}) — "
            "structure in this image is partly inferred, not measured. "
            "See the uncertainty overlay and the Tier A coadd for the measurement."
        )

    def summary(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "scale": self.scale,
            "model_id": self.model_id,
            "prior_contribution": self.prior_contribution,
            "label": self.label,
            "metrics": self.metrics,
            "notes": list(self.notes),
        }


class SuperResolver(ABC):
    """Interface every Tier B model implements."""

    architecture: str = "abstract"

    def __init__(self, scale: float = 2.0, device: str | None = None) -> None:
        self.scale = float(scale)
        self.device = device

    @abstractmethod
    def enhance(self, inputs: SRInputs) -> SRResult:
        """Produce an enhanced image plus a per-pixel uncertainty map."""

    @abstractmethod
    def load_weights(self, path: str) -> None:
        """Load trained weights (from the ``models`` table's S3 key)."""

    def describe(self) -> dict[str, Any]:
        return {"architecture": self.architecture, "scale": self.scale, "device": self.device}
