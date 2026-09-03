"""Spatially varying sky background.

Light pollution is additive, spatially structured and spectrally biased
(section 8 of the research note). It has to be modelled and subtracted
*before* the Zackay-Ofek weights are computed, because those weights are
``F_j / sigma_j**2`` and ``sigma_j`` is meaningless while a gradient of
several hundred ADU is still sitting in the frame.

We use :class:`photutils.background.Background2D` with a MAD-based estimator
and SExtractor-style mesh interpolation. The box size must be *much larger*
than the objects of interest or the model eats the nebula it is meant to sit
under; the default here is 1/8 of the frame, clamped to a sane range, and the
caller is expected to raise it for large extended targets.

One trade-off worth knowing before tuning: ``filter_size`` median-filters the
mesh itself, which is SExtractor's defence against a cell that happens to sit
on a bright object. It also flattens a genuinely steep gradient **within half
a box of the frame edge**, because the filter has no neighbours to one side.
On a synthetic frame with a strong linear ramp, ``filter_size=3`` leaves a
residual at the border several times larger than ``filter_size=1`` does, while
the interior is unaffected. Keep 3 for crowded or nebula-filled fields; drop to
1 when the sky is dominated by a smooth light-pollution ramp and the frame
edges matter.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrostack.io.frame import Frame

__all__ = ["BackgroundModel", "estimate_background", "subtract_background"]


@dataclass(slots=True)
class BackgroundModel:
    """The fitted sky, its RMS map, and summary numbers for the DB."""

    background: np.ndarray
    rms: np.ndarray
    median_level: float
    rms_median: float
    gradient_amplitude: float
    box_size: tuple[int, int]
    filter_size: tuple[int, int]

    def describe(self) -> dict[str, float | list[int]]:
        return {
            "median_level": self.median_level,
            "rms_median": self.rms_median,
            "gradient_amplitude": self.gradient_amplitude,
            "box_size": list(self.box_size),
            "filter_size": list(self.filter_size),
        }


def _auto_box(shape: tuple[int, int], requested: int | None) -> int:
    if requested:
        return max(8, int(requested))
    box = max(shape) // 8
    return int(np.clip(box, 16, 256))


def estimate_background(
    data: np.ndarray,
    mask: np.ndarray | None = None,
    box_size: int | None = None,
    filter_size: int = 3,
    sigma: float = 3.0,
    exclude_percentile: float = 90.0,
) -> BackgroundModel:
    """Fit a 2-D background with photutils.

    Falls back to a plain robust constant when the frame is too small for a
    sensible mesh, so that tiny synthetic test images still work.
    """
    arr = np.asarray(data, dtype=np.float32)
    box = _auto_box(arr.shape, box_size)
    filt = int(filter_size) | 1  # photutils wants an odd filter size

    if min(arr.shape) < 3 * box:
        box = max(8, min(arr.shape) // 3)
    if min(arr.shape) < 24:
        med = float(np.nanmedian(arr))
        mad = float(np.nanmedian(np.abs(arr - med))) * 1.4826
        return BackgroundModel(
            background=np.full(arr.shape, med, dtype=np.float32),
            rms=np.full(arr.shape, max(mad, 1e-6), dtype=np.float32),
            median_level=med,
            rms_median=max(mad, 1e-6),
            gradient_amplitude=0.0,
            box_size=(box, box),
            filter_size=(filt, filt),
        )

    from astropy.stats import SigmaClip
    from photutils.background import Background2D, MADStdBackgroundRMS, MedianBackground

    kwargs: dict[str, object] = {
        "box_size": (box, box),
        "filter_size": (filt, filt),
        "mask": mask,
        "sigma_clip": SigmaClip(sigma=sigma, maxiters=5),
        "bkg_estimator": MedianBackground(),
        "exclude_percentile": exclude_percentile,
    }
    # photutils renamed this keyword in 3.0 and will drop the old spelling in
    # 4.0. Pick whichever the installed version accepts rather than pinning the
    # library, since the reconstruction workers and the training box do not
    # always upgrade together.
    import inspect

    rms_key = (
        "bkg_rms_estimator"
        if "bkg_rms_estimator" in inspect.signature(Background2D).parameters
        else "bkgrms_estimator"
    )
    kwargs[rms_key] = MADStdBackgroundRMS()
    bkg = Background2D(arr, **kwargs)
    background = np.asarray(bkg.background, dtype=np.float32)
    rms = np.asarray(bkg.background_rms, dtype=np.float32)
    return BackgroundModel(
        background=background,
        rms=np.maximum(rms, 1e-6),
        median_level=float(np.median(background)),
        rms_median=float(np.median(rms)),
        gradient_amplitude=float(np.ptp(background)),
        box_size=(box, box),
        filter_size=(filt, filt),
    )


def subtract_background(
    frame: Frame,
    box_size: int | None = None,
    filter_size: int = 3,
    sigma: float = 3.0,
    keep_pedestal: bool = False,
) -> Frame:
    """Subtract the fitted sky and record its statistics on the frame.

    ``keep_pedestal=True`` adds the median level back, which keeps the data
    positive for Poisson-based deconvolution while still removing the
    *gradient*. Both variants are flux-preserving **for sources**: an additive
    constant does not change a source's integrated flux above background.
    """
    model = estimate_background(
        frame.data,
        mask=(~frame.good) if frame.mask is not None or frame.saturated is not None else None,
        box_size=box_size,
        filter_size=filter_size,
        sigma=sigma,
    )
    data = frame.data - model.background
    if keep_pedestal:
        data = data + model.median_level

    out = frame.copy_with(data.astype(np.float32), background=model.background)
    out.quality = frame.quality.model_copy(
        update={
            "background_adu": model.median_level,
            "background_rms": model.rms_median,
            "sky_gradient_amplitude": model.gradient_amplitude,
            "noise_sigma": model.rms_median,
        }
    )
    if frame.variance is None:
        out.variance = np.square(model.rms).astype(np.float32)
    out.note(
        "calibrate.background",
        f"Background2D box={model.box_size} level={model.median_level:.4g} "
        f"rms={model.rms_median:.4g} gradient={model.gradient_amplitude:.4g}",
        flux_preserving=True,
    )
    return out
