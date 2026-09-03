r"""Zackay & Ofek optimal coaddition.

References
----------
Zackay, B. & Ofek, E. O. 2017, *How to COAAD Images. I. Optimal Coaddition of
Point Sources*, ApJ 836, 187 (arXiv:1512.06872).

Zackay, B. & Ofek, E. O. 2017, *How to COAAD Images. II. A Coaddition Image
that is Optimal for Any Purpose in the Background-dominated Noise Limit*,
ApJ 836, 188 (arXiv:1512.06879).

Why this and not a weighted mean
--------------------------------
The research note is explicit (section 3): *this is the correct answer to "how
do I combine images of unequal quality?" and it is not a weighted average.*
A weighted mean throws away the fact that each frame has its **own PSF**. The
optimal statistic first applies a matched filter with each image's own PSF,
and only then sums, with weights proportional to transparency over variance.
For a corpus whose seeing varies by 5x and whose sky brightness varies by
100x, that difference is worth a large fraction of a magnitude in depth.

The equations, as implemented
-----------------------------
Model for image ``j``, already background subtracted and registered onto the
common grid::

    M_j = F_j * (T ⊗ P_j) + eps_j,     eps_j ~ N(0, sigma_j**2)

with ``T`` the true sky, ``P_j`` the unit-sum PSF, ``F_j`` the transparency
(flux zero point) and ``sigma_j`` the background noise.

*Paper I*, the matched-filter score image (its eq. 6-8), in Fourier space::

    S_hat = sum_j  (F_j / sigma_j**2) * conj(P_hat_j) * M_hat_j            (1)

*Paper II* normalises this into a sufficient statistic ``R`` whose noise is
white with unit variance (its eq. 7-9)::

    D      = sum_j  F_j**2 * |P_hat_j|**2 / sigma_j**2                     (2)
    R_hat  = S_hat / sqrt(D)                                               (3)
    F_R    = sqrt( sum_j F_j**2 / sigma_j**2 )                             (4)
    P_hat_R = sqrt(D) / F_R                                                (5)

``R`` then satisfies ``R = T ⊗ P_R * F_R + white noise of unit variance``, so
dividing by ``F_R`` puts it back on the input flux scale with a well-defined,
generally *narrower* effective PSF ``P_R`` and a uniform noise of
``sigma_R / F_R`` (``sigma_R`` is 1 in the ideal case; see note 1 for why the
implementation measures it instead of assuming it).

Sanity check that the code is required to reproduce (see
``tests/test_optimal_coadd.py``): for ``N`` identical images, ``P_hat_R``
collapses to ``|P_hat|`` and ``F_R = sqrt(N) F / sigma``, i.e. the textbook
``sqrt(N)`` depth gain with the input PSF preserved.

Practical departures from the paper, all deliberate and all declared
-------------------------------------------------------------------
1. **Regularisation.** Both numerator and denominator of (3) vanish at spatial
   frequencies where every PSF has rolled off, so (3) is a numerically
   dangerous 0/0. We add ``epsilon * max(D)`` under the square root. This
   slightly biases the very highest frequencies towards zero — the
   conservative direction, since those frequencies carry no measurement.

   The floor also means ``R``'s noise is no longer exactly unit variance:
   every mode's noise power is multiplied by ``D / (D + reg)``. The reported
   uncertainty therefore uses the *measured* variance

       Var[R] = (1 / (H*W)) * sum_k  D(k) / (D(k) + reg)

   rather than the ideal 1. For a typical PSF that is a 20-30% correction, so
   quoting the unregularised value would make every published SNR wrong by
   that factor. ``metrics['sigma_r']`` records it, and
   ``tests/test_snr_scaling.py`` asserts the declared uncertainty matches the
   scatter of a source-free coadd.
2. **Uniform sigma_j per frame.** The derivation assumes a spatially constant
   background noise. Ours varies (vignetting, gradients, partial coverage).
   We use the frame's median sigma for the filter and carry the *true*
   per-pixel coverage in the weight map, so partially-covered regions are
   correctly down-weighted even though the filter itself is stationary.
3. **Masked pixels are filled, not zeroed.** An FFT has no concept of a mask.
   Zeroing a satellite trail would ring across the whole frame. We fill each
   masked pixel with the inverse-variance mean of the *other* frames at that
   pixel, falling back to zero (the background) where no frame covers it.
4. **One PSF per frame.** Frames flagged ``psf_field_varying`` by
   :mod:`astrostack.align.stars` violate the stationarity assumption; they are
   reported in ``metrics['field_varying_psf_frames']`` so the caller can
   choose to tile the coaddition or drop them.
"""

from __future__ import annotations

import numpy as np

from astrostack.io.frame import Frame
from astrostack.logging import get_logger
from astrostack.stack.base import CoaddResult, align_psf_kernels, as_cube
from astrostack.stack.reject import combined_rejection

__all__ = ["OptimalCoaddDiagnostics", "optimal_coadd"]

log = get_logger(__name__)


class OptimalCoaddDiagnostics(dict):
    """Plain dict subclass so diagnostics serialise straight into provenance."""


def _frame_scalars(frames: list[Frame]) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(F_j, sigma_j)`` arrays.

    ``F_j`` is the transparency / flux zero point. A photometrically
    unreliable frame (a JPEG) has no trustworthy zero point, so its ``F_j``
    is pinned to 1.0 and it contributes only through its variance — it can
    add depth but it can never pull the flux scale.
    """
    f = np.ones(len(frames), dtype=np.float64)
    sigma = np.ones(len(frames), dtype=np.float64)
    for i, fr in enumerate(frames):
        t = fr.quality.transparency
        f[i] = 1.0 if (fr.meta.photometrically_unreliable or not t or t <= 0) else float(t)
        s = fr.quality.noise_sigma or fr.quality.background_rms
        if not s or s <= 0:
            v = fr.effective_variance()
            s = float(np.sqrt(np.nanmedian(v[np.isfinite(v) & (v > 0)]))) if np.any(v > 0) else 1.0
        sigma[i] = max(float(s), 1e-12)
    return f, sigma


def _fill_masked(cube: np.ndarray, var: np.ndarray, usable: np.ndarray) -> np.ndarray:
    """Replace unusable pixels with the inverse-variance mean of the others."""
    w = np.where(usable, 1.0 / np.maximum(var, 1e-30), 0.0)
    wsum = w.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        consensus = np.where(wsum > 0, (w * cube).sum(axis=0) / np.maximum(wsum, 1e-30), 0.0)
    return np.where(usable, cube, consensus[None, :, :]).astype(np.float64)


def optimal_coadd(
    frames: list[Frame],
    epsilon: float = 1e-4,
    reject: bool = True,
    sigma_low: float = 5.0,
    sigma_high: float = 3.0,
    reject_trails: bool = True,
    psf_output_size: int = 33,
    return_score: bool = True,
) -> CoaddResult:
    """Proper coaddition of Zackay & Ofek (2017), papers I and II.

    Parameters
    ----------
    frames
        Co-registered, background-subtracted frames, each with a measured PSF
        (:func:`astrostack.align.stars.characterise_frame`) and a noise sigma.
        Must be sorted by ``frame_id``.
    epsilon
        Fourier-space regularisation as a fraction of ``max(D)``; see note 1.
    return_score
        Also compute ``S_corr``, the matched-filter detection image in units
        of sigma. This is the map you threshold to find sources, and it is
        what ``metrics.injection`` measures recovery on.

    Returns
    -------
    CoaddResult
        ``image`` is ``R / F_R`` — on the input flux scale, with effective PSF
        ``psf``. ``uncertainty`` is ``sigma_R / F_R`` scaled by local coverage.
        ``metrics['score_image']`` holds ``S_corr`` when requested.
    """
    data, var, good = as_cube(frames)
    n, h, w = data.shape
    if n == 0:
        raise ValueError("optimal_coadd needs at least one frame")

    rejection = combined_rejection(
        data,
        good,
        method="sigma" if (reject and n >= 3) else "none",
        sigma_low=sigma_low,
        sigma_high=sigma_high,
        reject_trails=reject_trails and reject and n >= 3,
        scale=np.sqrt(np.maximum(var, 1e-12)),
    )
    usable = good & ~rejection.rejected

    f_j, sigma_j = _frame_scalars(frames)
    filled = _fill_masked(data.astype(np.float64), var.astype(np.float64), usable)

    psfs = align_psf_kernels(frames, (h, w))          # (N, H, W), centre at origin
    p_hat = np.fft.rfft2(psfs, axes=(1, 2))           # P_hat_j
    m_hat = np.fft.rfft2(filled, axes=(1, 2))         # M_hat_j

    inv_var = f_j / (sigma_j**2)                      # F_j / sigma_j^2
    s_hat = np.einsum("j,jkl->kl", inv_var, np.conjugate(p_hat) * m_hat)      # eq. (1)
    d = np.einsum("j,jkl->kl", (f_j**2) / (sigma_j**2), np.abs(p_hat) ** 2)   # eq. (2)

    d_max = float(d.max()) if d.size else 0.0
    reg = float(epsilon) * (d_max if d_max > 0 else 1.0)
    denom = np.sqrt(d + reg)

    r_hat = s_hat / denom                                                     # eq. (3)
    f_r = float(np.sqrt(np.sum((f_j**2) / (sigma_j**2))))                     # eq. (4)
    p_hat_r = denom / f_r                                                     # eq. (5)

    r_image = np.fft.irfft2(r_hat, s=(h, w))
    # R has unit-variance white noise by construction; divide by F_R to put it
    # back on the input flux scale.
    image_flux = (r_image / f_r).astype(np.float32)

    psf_r_full = np.fft.fftshift(np.fft.irfft2(p_hat_r, s=(h, w)))
    psf_r = _crop_centre(psf_r_full, psf_output_size)
    psf_r = np.clip(psf_r, 0.0, None)
    psf_sum = float(psf_r.sum())
    if psf_sum > 0:
        psf_r = (psf_r / psf_sum).astype(np.float32)
    else:  # pragma: no cover - degenerate input
        psf_r = None  # type: ignore[assignment]

    # Per-pixel coverage: the fraction of the ideal weight actually present.
    weight_per_pixel = np.einsum("j,jkl->kl", inv_var, usable.astype(np.float64))
    ideal_weight = float(inv_var.sum())
    coverage = weight_per_pixel / max(ideal_weight, 1e-30)

    sigma_r = float(np.sqrt(_noise_variance(d, reg, (h, w))))
    uncertainty = np.where(
        coverage > 0, (sigma_r / f_r) / np.sqrt(np.maximum(coverage, 1e-6)), 0.0
    ).astype(np.float32)
    image_flux = np.where(coverage > 0, image_flux, 0.0).astype(np.float32)

    metrics: dict[str, object] = {
        "f_r": f_r,
        "sigma_r": sigma_r,
        "epsilon": float(epsilon),
        "regularisation_absolute": reg,
        "coverage_fraction": float((coverage > 0).mean()),
        "rejection": rejection.describe(),
        "input_sigma": [round(float(s), 8) for s in sigma_j],
        "input_transparency": [round(float(v), 8) for v in f_j],
        "field_varying_psf_frames": [
            fr.frame_id for fr in frames if fr.psf is not None and fr.psf.is_field_varying
        ],
        "input_fwhm_pixels": [
            None if fr.psf is None else round(float(fr.psf.fwhm_pixels or np.nan), 6)
            for fr in frames
        ],
    }

    if return_score and psf_r is not None:
        # S_corr: matched-filter R (unit-variance white noise) with the coadd's
        # own PSF, normalised to unit L2 so the output is literally in sigma.
        k = np.asarray(psf_r, dtype=np.float64)
        norm = float(np.sqrt(np.sum(k**2)))
        if norm > 0:
            pad = np.zeros((h, w), dtype=np.float64)
            ky, kx = k.shape
            y0, x0 = (h - ky) // 2, (w - kx) // 2
            pad[y0 : y0 + ky, x0 : x0 + kx] = k / norm
            pad = np.roll(pad, (-(y0 + ky // 2), -(x0 + kx // 2)), axis=(0, 1))
            filt = np.conjugate(np.fft.rfft2(pad))
            score = np.fft.irfft2(np.fft.rfft2(r_image) * filt, s=(h, w))
            # Put the score in units of sigma, using the *exact* filtered noise
            # (the regularisation leaves R's noise slightly coloured, so
            # ||g||_2 * sigma_R would be wrong).
            score_sigma = _filtered_noise_sigma(filt, d, reg, (h, w))
            if score_sigma > 0:
                score = score / score_sigma
            metrics["score_image_available"] = True
            metrics["score_sigma"] = score_sigma
            score_arr = score.astype(np.float32)
        else:  # pragma: no cover
            score_arr = None
    else:
        score_arr = None

    result = CoaddResult(
        image=image_flux,
        weight=coverage.astype(np.float32),
        uncertainty=uncertainty,
        psf=psf_r,
        wcs=frames[0].wcs,
        method="zackay-ofek:proper-coadd",
        n_frames=n,
        flux_preserving=True,
        frame_weights={
            fr.frame_id: float(iv / max(inv_var.sum(), 1e-30))
            for fr, iv in zip(frames, inv_var, strict=True)
        },
        rejected_fraction={
            fr.frame_id: float(v)
            for fr, v in zip(frames, rejection.per_frame_fraction, strict=True)
        },
        metrics=metrics,
        notes=[
            "Zackay & Ofek 2017 (ApJ 836:187 and 836:188) proper coaddition: "
            "per-image PSF matched filter, weights F_j/sigma_j^2, explicit effective PSF",
            f"F_R = {f_r:.6g}; noise sigma_R/F_R = {sigma_r / f_r:.6g} before coverage scaling "
            f"(sigma_R = {sigma_r:.6g}, below 1 because the epsilon floor suppresses "
            "unmeasured frequencies)",
            f"Fourier regularisation epsilon={epsilon:g} suppresses frequencies where every "
            "input PSF has rolled off; nothing is measured there",
        ],
    )
    if score_arr is not None:
        result.metrics["score_image"] = score_arr
    return result


def _rfft_mode_weights(shape: tuple[int, int]) -> np.ndarray:
    """Multiplicity of each ``rfft2`` mode in the full complex spectrum.

    Columns 0 and (for even width) ``W//2`` appear once; every other column
    stands for a conjugate pair and therefore counts twice. The weights sum to
    ``H*W``, which is what makes a Parseval-style variance sum come out right.
    """
    h, w = shape
    n_cols = w // 2 + 1
    weights = np.full((h, n_cols), 2.0)
    weights[:, 0] = 1.0
    if w % 2 == 0:
        weights[:, -1] = 1.0
    return weights


def _noise_variance(d: np.ndarray, reg: float, shape: tuple[int, int]) -> float:
    r"""Per-pixel variance of ``R`` once the regularisation is accounted for.

    Without regularisation ``R`` has unit-variance white noise by
    construction. The ``epsilon`` floor multiplies the noise power of every
    mode by ``D / (D + reg)``, which is very close to 1 inside the passband
    and close to 0 outside it — so the true variance is::

        Var[R] = (1 / (H*W)) * sum_k  D(k) / (D(k) + reg)

    over the *full* spectrum. Reporting the unregularised 1 here would
    overstate the noise by 20-30% for a typical PSF, and every SNR the product
    quotes would be wrong by that factor.
    """
    if reg <= 0:
        return 1.0
    weights = _rfft_mode_weights(shape)
    ratio = d / (d + reg)
    return float(np.sum(weights * ratio) / (shape[0] * shape[1]))


def _filtered_noise_sigma(
    filter_hat: np.ndarray, d: np.ndarray, reg: float, shape: tuple[int, int]
) -> float:
    """Noise sigma after filtering ``R`` with ``filter_hat``.

    The regularisation leaves ``R``'s noise slightly coloured, so the scatter
    of a filtered version is not simply ``||g||_2 * sigma_R``. This evaluates
    it exactly.
    """
    weights = _rfft_mode_weights(shape)
    power = d / (d + reg) if reg > 0 else np.ones_like(d)
    var = float(np.sum(weights * np.abs(filter_hat) ** 2 * power) / (shape[0] * shape[1]))
    return float(np.sqrt(max(var, 0.0)))


def _crop_centre(arr: np.ndarray, size: int) -> np.ndarray:
    """Centre crop to an odd ``size``, or return ``arr`` if it is smaller."""
    size = max(int(size) | 1, 3)
    h, w = arr.shape
    if size >= min(h, w):
        return arr
    cy, cx = h // 2, w // 2
    r = size // 2
    return arr[cy - r : cy + r + 1, cx - r : cx + r + 1]
