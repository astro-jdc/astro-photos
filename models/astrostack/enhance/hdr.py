"""HDR compositing from the exposure spread.

Section 5's table: *"Different exposures -> unsaturated cores plus deep
outskirts (e.g. the Orion Trapezium). Easy, visually spectacular,
under-exploited."* A public repository has an enormous exposure spread for
free, from 1 s phone grabs to 10 min guided subs of the same object.

The composite is built on **linear** data, so this is not tone mapping: it is
a single estimate of the sky's surface brightness assembled from measurements
with different, partially-overlapping dynamic ranges.

Two steps:

1. **Relative scaling.** Each frame is put on a common flux scale by a robust
   regression against a reference, using only pixels that are unsaturated in
   *both* frames and above the noise in both. The regression is through the
   origin — the frames are background subtracted, so there is no offset to
   fit, and fitting one would absorb real sky signal.
2. **Weighted combination.** Each pixel's weight is its inverse variance
   multiplied by a confidence that falls to zero at the two ends of the usable
   range: near saturation (where the response is non-linear before it clips)
   and near the noise floor (where the measurement carries no information).

The output declares which pixels had their cores recovered exclusively from
short exposures, because those pixels have a different effective PSF and a
different noise model from the rest of the image.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrostack.io.frame import Frame
from astrostack.stack.base import CoaddResult

__all__ = ["HDRResult", "hdr_composite", "relative_scale", "saturation_level"]


def saturation_level(frame: Frame, percentile: float = 99.9) -> float:
    """Where this frame's response stops being linear, in its own units.

    In order of trustworthiness:

    1. the declared white level from the decoder (``extra['white_level']``);
    2. the smallest value among pixels the loader flagged as saturated — the
       clip level, measured;
    3. ``inf`` when nothing in the frame is saturated, because then there is
       no clipping to protect against.

    The tempting fourth option — a high percentile of the pixel values — is
    *wrong* and was the original bug here: on a star field the 99.9th
    percentile sits inside the brightest star's core, so every frame would
    report its brightest star as "saturated" and the composite would go to
    zero exactly where the HDR gain was supposed to be.
    """
    white = frame.extra.get("white_level")
    if white:
        return float(white)
    if frame.saturated is not None and frame.saturated.any():
        clipped = frame.data[frame.saturated]
        if clipped.size:
            return float(np.min(clipped))
    _ = percentile
    return float("inf")


@dataclass(slots=True)
class HDRResult:
    """The composite plus the bookkeeping needed to interpret it."""

    coadd: CoaddResult
    scales: dict[str, float]
    dynamic_range_stops: float
    recovered_core_fraction: float


def relative_scale(
    frame: Frame,
    reference: Frame,
    saturation_headroom: float = 0.9,
    min_pixels: int = 200,
) -> float:
    """Robust flux scale mapping ``frame`` onto ``reference``'s scale.

    Uses the median ratio (not least squares) so that a handful of saturated
    or cosmic-ray pixels that slipped through cannot drag the scale.
    """
    a = frame.data.astype(np.float64)
    b = reference.data.astype(np.float64)
    if a.shape != b.shape:
        raise ValueError("HDR scaling requires co-registered frames of the same shape")

    ok = frame.good & reference.good
    for fr, arr in ((frame, a), (reference, b)):
        if fr.saturated is not None:
            ok &= ~fr.saturated
        level = saturation_level(fr)
        if np.isfinite(level):
            ok &= arr < level * saturation_headroom

    sigma_a = frame.quality.noise_sigma or float(np.sqrt(np.nanmedian(frame.effective_variance())))
    sigma_b = reference.quality.noise_sigma or float(
        np.sqrt(np.nanmedian(reference.effective_variance()))
    )
    ok &= (a > 5.0 * max(sigma_a, 1e-9)) & (b > 5.0 * max(sigma_b, 1e-9))

    if ok.sum() < min_pixels:
        # Not enough overlap in the linear regime: fall back to exposure ratio.
        ea = frame.meta.exposure_seconds or 1.0
        eb = reference.meta.exposure_seconds or 1.0
        return float(eb / ea) if ea > 0 else 1.0
    return float(np.median(b[ok] / a[ok]))


def _confidence(
    data: np.ndarray,
    sigma: float,
    sat_level: float,
    knee: float = 0.75,
    noise_floor_sigma: float = 1.0,
) -> np.ndarray:
    """Per-pixel confidence in [0, 1]: zero at both ends of the usable range."""
    x = np.asarray(data, dtype=np.float64)
    if np.isfinite(sat_level):
        hi = np.clip((sat_level - x) / max(sat_level * (1.0 - knee), 1e-9), 0.0, 1.0)
    else:
        hi = np.ones(x.shape, dtype=np.float64)
    lo = np.clip(x / max(noise_floor_sigma * sigma, 1e-9), 0.0, 1.0)
    return (hi * lo).astype(np.float64)


def hdr_composite(
    frames: list[Frame],
    reference_index: int | None = None,
    saturation_percentile: float = 99.9,
    knee: float = 0.75,
) -> HDRResult:
    """Combine frames of very different exposure into one linear estimate.

    ``reference_index`` defaults to the frame with the *largest* number of
    unsaturated high-signal pixels, i.e. the one with the most usable dynamic
    range, rather than simply the longest exposure.
    """
    if len(frames) < 2:
        raise ValueError("HDR compositing needs at least two frames")
    ids = [f.frame_id for f in frames]
    if ids != sorted(ids):
        raise ValueError("frames must be sorted by frame_id")

    sat_levels = [saturation_level(fr, saturation_percentile) for fr in frames]

    if reference_index is None:
        usable_counts = [
            int(((fr.data > 0) & fr.good & (fr.data < lvl * knee)).sum())
            if np.isfinite(lvl)
            else int(((fr.data > 0) & fr.good).sum())
            for fr, lvl in zip(frames, sat_levels, strict=True)
        ]
        reference_index = int(np.argmax(usable_counts))
    reference = frames[reference_index]

    scales = {}
    num = np.zeros(reference.shape, dtype=np.float64)
    den = np.zeros(reference.shape, dtype=np.float64)
    var_num = np.zeros(reference.shape, dtype=np.float64)
    short_only = np.ones(reference.shape, dtype=bool)
    long_exposure_mask = np.zeros(reference.shape, dtype=bool)

    exposures = [fr.meta.exposure_seconds or 1.0 for fr in frames]
    median_exposure = float(np.median(exposures))

    for i, fr in enumerate(frames):
        s = 1.0 if i == reference_index else relative_scale(fr, reference)
        scales[fr.frame_id] = s
        sigma = fr.quality.noise_sigma or float(np.sqrt(np.nanmedian(fr.effective_variance())))
        conf = _confidence(fr.data, max(sigma, 1e-9), sat_levels[i], knee=knee)
        conf = np.where(fr.good, conf, 0.0)
        var = fr.effective_variance().astype(np.float64) * (s**2)
        w = conf / np.maximum(var, 1e-30)
        num += w * (fr.data.astype(np.float64) * s)
        den += w
        var_num += (w**2) * var
        if exposures[i] >= median_exposure:
            long_exposure_mask |= conf > 0
            short_only &= ~(conf > 0)

    covered = den > 0
    image = np.divide(num, den, out=np.zeros_like(num), where=covered).astype(np.float32)
    uncertainty = np.sqrt(
        np.divide(var_num, np.square(den), out=np.zeros_like(num), where=covered)
    ).astype(np.float32)

    recovered = short_only & covered
    finite = image[np.isfinite(image) & (image > 0)]
    if finite.size:
        stops = float(np.log2(np.percentile(finite, 99.99) / max(np.percentile(finite, 1.0), 1e-9)))
    else:
        stops = 0.0

    coadd = CoaddResult(
        image=image,
        weight=den.astype(np.float32),
        uncertainty=uncertainty,
        psf=None,
        wcs=reference.wcs,
        method="hdr-composite",
        n_frames=len(frames),
        flux_preserving=True,
        frame_weights=scales,
        metrics={
            "reference_frame": reference.frame_id,
            "dynamic_range_stops": stops,
            "recovered_core_fraction": float(recovered.mean()),
            "saturation_levels": {
                fr.frame_id: lvl for fr, lvl in zip(frames, sat_levels, strict=True)
            },
        },
        notes=[
            "HDR composite on linear data: frames rescaled by robust median ratio, "
            "combined with inverse-variance weights tapered at saturation and at the noise floor",
            "pixels flagged 'recovered core' come only from short exposures and therefore "
            "carry a different effective PSF and noise model from the rest of the image",
        ],
    )
    return HDRResult(
        coadd=coadd,
        scales=scales,
        dynamic_range_stops=stops,
        recovered_core_fraction=float(recovered.mean()),
    )
