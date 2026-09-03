"""Synthetic source injection and recovery curves.

Rule 4 of the astro-ml brief: *nothing generated without an audit.* This
module is that audit, and it is the single most important file in
:mod:`astrostack.metrics`.

The procedure is the standard completeness experiment of survey astronomy,
and it is the one thing that distinguishes a real reconstruction from a
pretty hallucination:

1. Inject point sources of **known flux** at **known positions** into the
   individual input frames, convolved with each frame's own measured PSF and
   placed through each frame's own WCS so that they land on the same patch of
   sky in every frame — exactly as a real star would.
2. Run the pipeline under test on the injected frames.
3. Measure the recovered flux with matched-filter photometry using the
   coadd's own effective PSF.
4. Fit ``recovered = slope * injected + intercept``.

What the numbers mean:

* ``slope ~ 1`` and ``intercept ~ 0`` -> the pipeline is **linear in flux**.
  This is the property that makes the output usable for photometry, and it is
  the property that generative models silently break.
* ``slope < 1`` -> flux is being lost (over-aggressive rejection, a background
  model eating the sources).
* ``slope > 1`` or a large positive ``intercept`` -> the pipeline is *adding*
  flux. In astronomy that is not an aesthetic defect, it is a false
  discovery.
* ``completeness_50`` -> the flux at which half the injected sources are
  recovered above the detection threshold: the pipeline's real depth.

The empty-field control (:func:`false_positive_rate`) is the other half: run
the same measurement on pure noise and count what gets "detected".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from astrostack.io.frame import Frame
from astrostack.metrics.quality import matched_filter_snr
from astrostack.rng import generator

__all__ = [
    "InjectedSource",
    "InjectionReport",
    "false_positive_rate",
    "inject_into_frames",
    "injection_experiment",
    "measure_matched_flux",
    "plan_injection_grid",
]


@dataclass(slots=True)
class InjectedSource:
    """One synthetic source, in output-grid pixel coordinates."""

    x: float
    y: float
    flux: float

    @property
    def instrumental_magnitude(self) -> float:
        return float(-2.5 * np.log10(max(self.flux, 1e-30)))


@dataclass(slots=True)
class InjectionReport:
    """The recovery curve and everything derived from it."""

    injected_flux: np.ndarray
    recovered_flux: np.ndarray
    snr: np.ndarray
    detected: np.ndarray
    slope: float
    intercept: float
    r_squared: float
    completeness_50: float | None
    detection_threshold: float
    n_sources: int
    notes: list[str] = field(default_factory=list)

    @property
    def is_linear(self) -> bool:
        """True when the pipeline neither loses nor invents flux."""
        return bool(abs(self.slope - 1.0) < 0.15 and self.r_squared > 0.95)

    def as_dict(self) -> dict[str, object]:
        return {
            "n_sources": self.n_sources,
            "slope": self.slope,
            "intercept": self.intercept,
            "r_squared": self.r_squared,
            "completeness_50": self.completeness_50,
            "detection_threshold_sigma": self.detection_threshold,
            "detection_fraction": float(np.mean(self.detected)) if self.detected.size else 0.0,
            "is_linear": self.is_linear,
            "notes": list(self.notes),
            "curve": [
                {"injected": float(i), "recovered": float(r), "snr": float(s), "detected": bool(d)}
                for i, r, s, d in zip(
                    self.injected_flux, self.recovered_flux, self.snr, self.detected, strict=True
                )
            ],
        }


def plan_injection_grid(
    shape: tuple[int, int],
    n_sources: int,
    fluxes: np.ndarray,
    seed: int,
    margin: int = 16,
    label: str = "injection",
) -> list[InjectedSource]:
    """Lay sources out on a jittered lattice, deterministically.

    A lattice (rather than uniform random positions) guarantees the sources do
    not pile up, and the jitter guarantees they do not all land on the same
    sub-pixel phase, which would flatter a drizzle-based pipeline.
    """
    rng = generator(seed, label, "positions")
    h, w = shape
    n = int(n_sources)
    cols = int(np.ceil(np.sqrt(n * w / max(h, 1))))
    cols = max(cols, 1)
    rows = int(np.ceil(n / cols))
    ys = np.linspace(margin, h - margin - 1, rows)
    xs = np.linspace(margin, w - margin - 1, cols)

    sources: list[InjectedSource] = []
    fluxes = np.asarray(fluxes, dtype=np.float64).ravel()
    k = 0
    for y in ys:
        for x in xs:
            if k >= n:
                break
            jy = float(y + rng.uniform(-0.5, 0.5))
            jx = float(x + rng.uniform(-0.5, 0.5))
            sources.append(InjectedSource(x=jx, y=jy, flux=float(fluxes[k % fluxes.size])))
            k += 1
    return sources


def _add_psf(image: np.ndarray, psf: np.ndarray, y: float, x: float, flux: float) -> None:
    """Add ``flux * psf`` centred at the sub-pixel position ``(y, x)``.

    The sub-pixel shift is done by Fourier phase ramp on the kernel, which is
    exact for a band-limited kernel and therefore does not itself blur the
    injected source — important, because a blurred injection would make the
    pipeline look better than it is.
    """
    k = np.asarray(psf, dtype=np.float64)
    ky, kx = k.shape
    iy, ix = int(np.floor(y)), int(np.floor(x))
    fy, fx = float(y - iy), float(x - ix)

    ku = np.fft.fft2(k)
    v = np.fft.fftfreq(ky)[:, None]
    u = np.fft.fftfreq(kx)[None, :]
    shifted = np.real(np.fft.ifft2(ku * np.exp(-2j * np.pi * (v * fy + u * fx))))
    shifted = np.clip(shifted, 0.0, None)
    s = shifted.sum()
    if s <= 0:
        return
    shifted = shifted / s * float(flux)

    y0, x0 = iy - ky // 2, ix - kx // 2
    ys, xs = max(y0, 0), max(x0, 0)
    ye, xe = min(y0 + ky, image.shape[0]), min(x0 + kx, image.shape[1])
    if ye <= ys or xe <= xs:
        return
    image[ys:ye, xs:xe] += shifted[ys - y0 : ye - y0, xs - x0 : xe - x0]


def inject_into_frames(
    frames: list[Frame],
    sources: list[InjectedSource],
    reference_wcs=None,
    add_shot_noise: bool = True,
    seed: int = 0,
    gain_e_per_adu: float = 1.0,
) -> list[Frame]:
    """Inject sources into every frame, at the same sky position.

    Each frame gets the source convolved with **its own** PSF, so a bad-seeing
    frame receives a bad-seeing star. If ``reference_wcs`` is given (and the
    frames are solved), positions are converted sky-side; otherwise the same
    pixel coordinates are used, which is correct for already-registered
    frames.
    """
    out: list[Frame] = []
    for idx, fr in enumerate(frames):
        if fr.psf is None:
            raise ValueError(f"{fr.frame_id}: injection needs a measured PSF")
        data = fr.data.astype(np.float64, copy=True)
        kernel = np.asarray(fr.psf.normalised(), dtype=np.float64)
        rng = generator(seed, "injection", "shot", fr.frame_id)

        for src in sources:
            y, x = src.y, src.x
            if reference_wcs is not None and fr.wcs is not None:
                sky = reference_wcs.pixel_to_world(src.x, src.y)
                px, py = fr.wcs.world_to_pixel(sky)
                if not (np.isfinite(px) and np.isfinite(py)):
                    continue
                y, x = float(py), float(px)
            before = data.sum()
            _add_psf(data, kernel, y, x, src.flux)
            if add_shot_noise and data.sum() > before:
                added = data.sum() - before
                _ = added  # shot noise applied below on the whole increment

        if add_shot_noise:
            increment = np.clip(data - fr.data.astype(np.float64), 0.0, None)
            noisy = rng.poisson(increment * gain_e_per_adu) / gain_e_per_adu
            data = fr.data.astype(np.float64) + noisy

        new = fr.copy_with(data.astype(np.float32))
        new.note("metrics.injection", f"{len(sources)} synthetic sources injected", True)
        new.extra = dict(fr.extra)
        new.extra["injected"] = True
        out.append(new)
        _ = idx
    return out


def measure_matched_flux(
    image: np.ndarray,
    psf: np.ndarray,
    positions: np.ndarray,
    noise: np.ndarray | float,
) -> np.ndarray:
    r"""Maximum-likelihood flux at each position.

    ``f_hat = sum(p*d/sigma**2) / sum(p**2/sigma**2)``, the matched-filter
    estimator. Unbiased when the PSF is right and the background is zero, and
    it is the same statistic the SNR uses, so the two are consistent.
    """
    d = np.asarray(image, dtype=np.float64)
    pos = np.atleast_2d(np.asarray(positions, dtype=np.float64))
    if np.isscalar(noise) or np.ndim(noise) == 0:
        sigma_map = np.full(d.shape, float(noise), dtype=np.float64)
    else:
        sigma_map = np.asarray(noise, dtype=np.float64)

    k = np.asarray(psf, dtype=np.float64)
    ky, kx = k.shape
    out = np.full(pos.shape[0], np.nan, dtype=np.float64)
    for i, (y, x) in enumerate(pos):
        iy, ix = round(y), round(x)
        y0, x0 = iy - ky // 2, ix - kx // 2
        ys, xs = max(y0, 0), max(x0, 0)
        ye, xe = min(y0 + ky, d.shape[0]), min(x0 + kx, d.shape[1])
        if ye <= ys or xe <= xs:
            continue
        stamp = k[ys - y0 : ye - y0, xs - x0 : xe - x0]
        s = np.maximum(sigma_map[ys:ye, xs:xe], 1e-12)
        inv_var = 1.0 / (s * s)
        den = float(np.sum(stamp * stamp * inv_var))
        if den <= 0:
            continue
        out[i] = float(np.sum(stamp * d[ys:ye, xs:xe] * inv_var)) / den
    return out


def injection_experiment(
    frames: list[Frame],
    combiner,
    fluxes: np.ndarray,
    n_sources: int = 36,
    seed: int = 12345,
    detection_threshold: float = 5.0,
    reference_wcs=None,
    add_shot_noise: bool = True,
) -> InjectionReport:
    """Full inject -> stack -> recover experiment.

    ``combiner`` takes a list of frames and returns a
    :class:`~astrostack.stack.base.CoaddResult`.
    """
    if not frames:
        raise ValueError("injection_experiment needs frames")
    shape = frames[0].shape
    sources = plan_injection_grid(shape, n_sources, fluxes, seed)
    injected = inject_into_frames(
        frames, sources, reference_wcs=reference_wcs, add_shot_noise=add_shot_noise, seed=seed
    )

    baseline = combiner(sorted(frames, key=lambda f: f.frame_id))
    result = combiner(sorted(injected, key=lambda f: f.frame_id))
    if result.psf is None:
        raise ValueError("the combiner must return an effective PSF for the audit to be possible")

    positions = np.array([[s.y, s.x] for s in sources], dtype=np.float64)
    noise = result.uncertainty if result.uncertainty is not None else 1.0
    if isinstance(noise, np.ndarray):
        med = float(np.median(noise[noise > 0])) if np.any(noise > 0) else 1.0
        noise = np.where(noise > 0, noise, med)

    # Difference imaging removes the real field, so only the injected flux is
    # measured. This is what keeps the slope honest on a crowded field.
    delta = result.image.astype(np.float64) - baseline.image.astype(np.float64)
    recovered = measure_matched_flux(delta, result.psf, positions, noise)
    snr = matched_filter_snr(delta, result.psf, positions, noise)

    injected_flux = np.array([s.flux for s in sources], dtype=np.float64)
    detected = np.asarray(snr >= detection_threshold, dtype=bool)

    ok = np.isfinite(recovered) & np.isfinite(injected_flux)
    if ok.sum() >= 2:
        slope, intercept = np.polyfit(injected_flux[ok], recovered[ok], 1)
        pred = slope * injected_flux[ok] + intercept
        ss_res = float(np.sum((recovered[ok] - pred) ** 2))
        ss_tot = float(np.sum((recovered[ok] - recovered[ok].mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    else:  # pragma: no cover
        slope, intercept, r2 = float("nan"), float("nan"), float("nan")

    completeness_50 = None
    uniq = np.unique(injected_flux)
    if uniq.size >= 2:
        frac = np.array([float(detected[injected_flux == f].mean()) for f in uniq])
        above = np.flatnonzero(frac >= 0.5)
        if above.size:
            completeness_50 = float(uniq[above[0]])

    notes = []
    if abs(slope - 1.0) > 0.15:
        notes.append(
            f"recovery slope {slope:.3f} deviates from unity by more than 15%: the pipeline "
            f"{'loses' if slope < 1 else 'ADDS'} flux and must not be used for photometry"
        )
    if intercept > 0 and np.isfinite(intercept):
        rel = intercept / max(float(np.median(injected_flux)), 1e-30)
        if rel > 0.1:
            notes.append(
                f"positive intercept {intercept:.4g} ({rel:.1%} of the median injected flux): "
                "the pipeline is adding flux where none was injected"
            )

    return InjectionReport(
        injected_flux=injected_flux,
        recovered_flux=recovered,
        snr=snr,
        detected=detected,
        slope=float(slope),
        intercept=float(intercept),
        r_squared=float(r2),
        completeness_50=completeness_50,
        detection_threshold=float(detection_threshold),
        n_sources=len(sources),
        notes=notes,
    )


def false_positive_rate(
    image: np.ndarray,
    psf: np.ndarray,
    noise: np.ndarray | float | None = None,
    threshold: float = 5.0,
) -> dict[str, float]:
    """Count matched-filter detections in an image that should be empty.

    Used by ``tests/test_empty_field.py``: pure noise in, nothing detectable
    out. A pipeline that manufactures sources fails here, loudly.

    ``noise=None`` (the default) calibrates the threshold on the **filtered
    detection image itself**, which is what a real survey does and what any
    resampled product requires: drizzle, reprojection and deconvolution all
    leave the noise *correlated*, so a per-pixel sigma combined with the white
    noise assumption ``sigma * ||p||_2`` understates the filtered scatter and
    manufactures significance out of bookkeeping. Passing an explicit ``noise``
    keeps the white-noise convention, which is correct only for an unresampled
    image.
    """
    from scipy.ndimage import maximum_filter
    from scipy.signal import fftconvolve

    from astrostack.robust import robust_sigma

    d = np.asarray(image, dtype=np.float64)
    k = np.asarray(psf, dtype=np.float64)
    norm = float(np.sqrt(np.sum(k**2)))
    if norm <= 0:
        raise ValueError("degenerate PSF")

    filtered = fftconvolve(d, k[::-1, ::-1] / norm, mode="same")
    if noise is None:
        sigma = robust_sigma(filtered)
        if not np.isfinite(sigma) or sigma <= 0:  # pragma: no cover
            sigma = float(np.std(filtered)) or 1.0
        calibration = "filtered-image"
    elif np.isscalar(noise) or np.ndim(noise) == 0:
        sigma = float(noise)
        calibration = "supplied-scalar"
    else:
        arr = np.asarray(noise, dtype=np.float64)
        sigma = float(np.median(arr[arr > 0])) if np.any(arr > 0) else 1.0
        calibration = "supplied-map"
    score = filtered / max(sigma, 1e-12)

    peaks = (score == maximum_filter(score, size=5)) & (score >= threshold)
    # Ignore the border, where the convolution is not fully supported.
    border = max(k.shape) // 2 + 1
    interior = np.zeros(peaks.shape, dtype=bool)
    interior[border:-border, border:-border] = True
    peaks &= interior

    n_pixels = int(interior.sum())
    return {
        "n_detections": float(peaks.sum()),
        "max_score_sigma": float(np.max(score[interior])) if n_pixels else float("nan"),
        "threshold": float(threshold),
        "sigma": float(sigma),
        "noise_calibration": calibration,  # type: ignore[dict-item]
        "n_independent_beams": float(n_pixels / max(np.sum(k) ** 2 / np.sum(k**2), 1.0)),
    }
