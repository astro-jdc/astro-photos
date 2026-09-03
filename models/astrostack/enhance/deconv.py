r"""Deconvolution with a *measured* PSF, and an explicit honesty budget.

Section 5 of the research note sets the boundary this module lives inside:

    Deconvolution can partially undo the blur *inside* the optical passband
    and can, with strong priors, extrapolate modestly outside it — but the
    optical transfer function is exactly zero beyond the cutoff, so anything
    reconstructed there is prior, not measurement.

and section 5's table budgets the honest gain at **~1.5-2x FWHM reduction with
high SNR and a well-measured PSF**, prior-dependent beyond that.

So both algorithms here take a bounded iteration/regularisation budget and
return a :class:`DeconvolutionResult` that says, in numbers, how much of the
sharpening is still measurement:

* ``achieved_fwhm_pixels`` — measured by running the *same* deconvolution on a
  synthetic point source put through the *same* PSF. This is not an estimate,
  it is the actual resolution of the operator as configured.
* ``diffraction_limit_pixels`` — 1.22 lambda/D for the contributing aperture.
* ``prior_dominated`` — ``True`` once the achieved FWHM goes below the
  diffraction limit, i.e. once the output contains structure at spatial
  frequencies where the telescope measured exactly nothing.

Richardson-Lucy conserves total flux (the multiplicative update preserves the
sum for a unit-sum PSF with periodic boundaries). Wiener filtering does not in
general, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import fftconvolve

from astrostack.logging import get_logger

__all__ = [
    "DeconvolutionResult",
    "measure_operator_fwhm",
    "operator_psf",
    "richardson_lucy",
    "wiener_deconvolve",
]

log = get_logger(__name__)
_FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


@dataclass(slots=True)
class DeconvolutionResult:
    """A deconvolved image plus the audit of how far it can be trusted."""

    image: np.ndarray
    method: str
    iterations: int
    input_fwhm_pixels: float | None = None
    achieved_fwhm_pixels: float | None = None
    diffraction_limit_pixels: float | None = None
    prior_dominated: bool = False
    flux_ratio: float | None = None
    flux_preserving: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def sharpening_factor(self) -> float | None:
        if self.input_fwhm_pixels and self.achieved_fwhm_pixels:
            return float(self.input_fwhm_pixels / self.achieved_fwhm_pixels)
        return None

    def summary(self) -> dict[str, object]:
        return {
            "method": self.method,
            "iterations": self.iterations,
            "input_fwhm_pixels": self.input_fwhm_pixels,
            "achieved_fwhm_pixels": self.achieved_fwhm_pixels,
            "sharpening_factor": self.sharpening_factor,
            "diffraction_limit_pixels": self.diffraction_limit_pixels,
            "prior_dominated": self.prior_dominated,
            "flux_ratio": self.flux_ratio,
            "flux_preserving": self.flux_preserving,
            "warnings": list(self.warnings),
        }


def _gaussian_fwhm(image: np.ndarray) -> float:
    """FWHM of a centred, positive profile from its second moment."""
    a = np.clip(np.asarray(image, dtype=np.float64), 0.0, None)
    total = a.sum()
    if total <= 0:
        return float("nan")
    h, w = a.shape
    yy, xx = np.mgrid[0:h, 0:w]
    cy = float((a * yy).sum() / total)
    cx = float((a * xx).sum() / total)
    varx = float((a * (xx - cx) ** 2).sum() / total)
    vary = float((a * (yy - cy) ** 2).sum() / total)
    sigma = np.sqrt(max((varx + vary) / 2.0, 1e-12))
    return float(_FWHM_PER_SIGMA * sigma)


def operator_psf(
    psf: np.ndarray, method: str, size: int = 65, **kwargs: object
) -> np.ndarray:
    """The **effective PSF of the deconvolved image**, measured not assumed.

    Puts a unit point source through ``psf`` (noiselessly) and runs the very
    same operator, with the very same parameters, on the result. What comes
    out *is* the operator's impulse response. Downstream matched filtering
    must use this, not the original PSF: after deconvolution the image no
    longer has the PSF it started with, and filtering with the old one both
    loses signal and mis-states significance.
    """
    size = max(int(size) | 1, 2 * max(psf.shape) + 1)
    scene = np.zeros((size, size), dtype=np.float64)
    scene[size // 2, size // 2] = 1.0
    blurred = fftconvolve(scene, np.asarray(psf, dtype=np.float64), mode="same")
    if method == "richardson-lucy":
        out = _rl_core(blurred, psf, int(kwargs.get("iterations", 20)), float(kwargs.get("damping", 0.0)))  # type: ignore[arg-type]
    elif method == "wiener":
        out = _wiener_core(blurred, psf, float(kwargs.get("nsr", 1e-3)))  # type: ignore[arg-type]
    else:
        raise ValueError(f"unknown method {method!r}")
    out = np.clip(out, 0.0, None)
    total = float(out.sum())
    return (out / total).astype(np.float32) if total > 0 else out.astype(np.float32)


def measure_operator_fwhm(
    psf: np.ndarray, method: str, size: int = 65, **kwargs: object
) -> float:
    """FWHM of :func:`operator_psf`: the resolution the operator delivers."""
    return _gaussian_fwhm(operator_psf(psf, method, size, **kwargs))


def _rl_core(image: np.ndarray, psf: np.ndarray, iterations: int, damping: float = 0.0) -> np.ndarray:
    d = np.asarray(image, dtype=np.float64)
    p = np.asarray(psf, dtype=np.float64)
    p = p / p.sum()
    p_flip = p[::-1, ::-1]
    # RL is derived for Poisson data and needs a non-negative observation.
    offset = 0.0
    dmin = float(d.min())
    if dmin < 0:
        offset = -dmin
        d = d + offset
    est = np.full(d.shape, max(float(d.mean()), 1e-12), dtype=np.float64)
    eps = 1e-12
    for _ in range(max(int(iterations), 0)):
        conv = fftconvolve(est, p, mode="same")
        ratio = d / np.maximum(conv, eps)
        if damping > 0:
            # Damped RL (White 1994): suppress the update where the residual is
            # within the noise, which is what stops RL amplifying background
            # grain into fake knots.
            ratio = 1.0 + (ratio - 1.0) / (1.0 + damping)
        est = est * fftconvolve(ratio, p_flip, mode="same")
        est = np.clip(est, 0.0, None)
    return est - offset


def _wiener_core(image: np.ndarray, psf: np.ndarray, nsr: float) -> np.ndarray:
    d = np.asarray(image, dtype=np.float64)
    h, w = d.shape
    p = np.asarray(psf, dtype=np.float64)
    p = p / p.sum()
    pad = np.zeros((h, w), dtype=np.float64)
    ky, kx = p.shape
    y0, x0 = (h - ky) // 2, (w - kx) // 2
    if y0 < 0 or x0 < 0:
        raise ValueError("PSF is larger than the image")
    pad[y0 : y0 + ky, x0 : x0 + kx] = p
    pad = np.roll(pad, (-(y0 + ky // 2), -(x0 + kx // 2)), axis=(0, 1))
    p_hat = np.fft.rfft2(pad)
    d_hat = np.fft.rfft2(d)
    filt = np.conjugate(p_hat) / (np.abs(p_hat) ** 2 + float(nsr))
    return np.fft.irfft2(d_hat * filt, s=(h, w))


def richardson_lucy(
    image: np.ndarray,
    psf: np.ndarray,
    iterations: int = 20,
    damping: float = 0.0,
    max_iterations: int = 60,
    diffraction_limit_pixels: float | None = None,
    input_fwhm_pixels: float | None = None,
) -> DeconvolutionResult:
    """Bounded Richardson-Lucy deconvolution.

    ``iterations`` is hard-capped at ``max_iterations``. There is no
    "just run it longer" mode: RL past a few tens of iterations on real data
    stops converging towards the scene and starts converging towards the
    noise realisation, producing the granular fake structure that gives
    deconvolution its bad name in amateur astronomy.
    """
    warnings: list[str] = []
    if iterations > max_iterations:
        warnings.append(
            f"requested {iterations} iterations, capped at {max_iterations}: beyond that "
            "Richardson-Lucy amplifies the noise realisation, not the scene"
        )
        iterations = max_iterations

    out = _rl_core(image, psf, iterations, damping)

    total_in = float(np.sum(np.asarray(image, dtype=np.float64)))
    total_out = float(np.sum(out))
    flux_ratio = total_out / total_in if abs(total_in) > 1e-12 else None

    achieved = measure_operator_fwhm(psf, "richardson-lucy", iterations=iterations, damping=damping)
    in_fwhm = input_fwhm_pixels if input_fwhm_pixels is not None else _gaussian_fwhm(psf)

    prior_dominated = False
    if diffraction_limit_pixels and np.isfinite(achieved) and achieved < diffraction_limit_pixels:
        prior_dominated = True
        warnings.append(
            f"achieved FWHM {achieved:.2f} px is below the diffraction limit "
            f"{diffraction_limit_pixels:.2f} px: structure at those frequencies is prior, "
            "not measurement, and the output must be labelled as such"
        )
    if in_fwhm and np.isfinite(achieved) and achieved > 0 and in_fwhm / achieved > 2.0:
        warnings.append(
            f"sharpening factor {in_fwhm / achieved:.2f}x exceeds the ~2x that the physics "
            "supports at high SNR; treat the excess as prior-driven"
        )

    return DeconvolutionResult(
        image=out.astype(np.float32),
        method="richardson-lucy",
        iterations=iterations,
        input_fwhm_pixels=float(in_fwhm) if np.isfinite(in_fwhm) else None,
        achieved_fwhm_pixels=float(achieved) if np.isfinite(achieved) else None,
        diffraction_limit_pixels=diffraction_limit_pixels,
        prior_dominated=prior_dominated,
        flux_ratio=flux_ratio,
        flux_preserving=True,
        warnings=warnings,
    )


def wiener_deconvolve(
    image: np.ndarray,
    psf: np.ndarray,
    nsr: float = 1e-3,
    noise_sigma: float | None = None,
    signal_power: float | None = None,
    diffraction_limit_pixels: float | None = None,
    input_fwhm_pixels: float | None = None,
) -> DeconvolutionResult:
    """Wiener deconvolution with a measured PSF.

    ``nsr`` is the noise-to-signal power ratio. When ``noise_sigma`` is given
    it is derived from the data instead: ``nsr = sigma**2 / signal_power``,
    with ``signal_power`` defaulting to the variance of the image. That is the
    statistically meaningful setting; a hand-tuned ``nsr`` is a sharpening
    knob, not an inverse-problem solution, and is flagged as such.
    """
    warnings: list[str] = []
    if noise_sigma is not None:
        power = signal_power if signal_power is not None else float(np.var(image))
        nsr = float(noise_sigma**2 / max(power, 1e-12))
    else:
        warnings.append("nsr supplied by hand rather than derived from a measured noise level")

    out = _wiener_core(image, psf, nsr)
    total_in = float(np.sum(np.asarray(image, dtype=np.float64)))
    total_out = float(np.sum(out))
    flux_ratio = total_out / total_in if abs(total_in) > 1e-12 else None

    achieved = measure_operator_fwhm(psf, "wiener", nsr=nsr)
    in_fwhm = input_fwhm_pixels if input_fwhm_pixels is not None else _gaussian_fwhm(psf)

    prior_dominated = False
    if diffraction_limit_pixels and np.isfinite(achieved) and achieved < diffraction_limit_pixels:
        prior_dominated = True
        warnings.append(
            f"achieved FWHM {achieved:.2f} px is below the diffraction limit "
            f"{diffraction_limit_pixels:.2f} px: prior, not measurement"
        )
    warnings.append(
        "Wiener filtering is a linear MMSE estimator, not a flux-conserving operator; "
        "photometry on the result must be recalibrated"
    )

    return DeconvolutionResult(
        image=out.astype(np.float32),
        method="wiener",
        iterations=1,
        input_fwhm_pixels=float(in_fwhm) if np.isfinite(in_fwhm) else None,
        achieved_fwhm_pixels=float(achieved) if np.isfinite(achieved) else None,
        diffraction_limit_pixels=diffraction_limit_pixels,
        prior_dominated=prior_dominated,
        flux_ratio=flux_ratio,
        flux_preserving=False,
        warnings=warnings,
    )
