"""Quality metrics for a reconstruction.

These are the numbers that land in ``reconstructions.metrics``. Two rules
govern the module:

* **A fair SNR comparison uses each image's own PSF.** Comparing a
  Zackay-Ofek coadd against a sigma-clipped mean by looking at peak pixel
  values is meaningless — the two have different effective PSFs. The honest
  comparison is the matched-filter detection significance of the *same*
  source, each image filtered with its *own* PSF and its own noise map. That
  is what :func:`matched_filter_snr` does, and it is what
  ``tests/test_optimal_coadd.py`` uses.
* **Flux conservation is measured, not asserted.** :func:`flux_ratio` is
  applied at stage boundaries so a regression shows up as a number.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrostack.align.stars import detect_sources

__all__ = [
    "MeasuredQuality",
    "depth_curve",
    "effective_pixel_scale",
    "flux_ratio",
    "matched_filter_snr",
    "measure_fwhm",
    "noise_equivalent_fwhm",
    "snr_gain_db",
]

_FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


@dataclass(slots=True)
class MeasuredQuality:
    """Summary of an image's measurable quality."""

    fwhm_pixels: float | None
    fwhm_arcsec: float | None
    eccentricity: float | None
    star_count: int
    background_rms: float
    pixel_scale_arcsec: float | None

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "fwhm_pixels": self.fwhm_pixels,
            "fwhm_arcsec": self.fwhm_arcsec,
            "eccentricity": self.eccentricity,
            "star_count": self.star_count,
            "background_rms": self.background_rms,
            "effective_pixel_scale_arcsec": self.pixel_scale_arcsec,
        }


def noise_equivalent_fwhm(psf: np.ndarray) -> float:
    r"""Width of a unit-sum PSF, in pixels, from its L2 norm.

    For a normalised Gaussian, ``||P||_2 = 1 / (2 sigma sqrt(pi))``, so

        FWHM = 2 sqrt(2 ln 2) * sigma = sqrt(2 ln 2 / pi) / ||P||_2

    This "noise-equivalent" width is preferable to a second-moment FWHM for
    comparing PSFs, for two reasons: it is exactly the quantity that sets
    point-source detection SNR (``SNR = f ||P||_2 / sigma``), and it is finite
    for a Moffat profile, whose second moment diverges. A second-moment FWHM
    of a Moffat is dominated by the wings and by wherever the kernel happened
    to be truncated, which makes it useless as a comparison.
    """
    p = np.asarray(psf, dtype=np.float64)
    total = float(p.sum())
    if total <= 0:
        return float("nan")
    p = p / total
    l2 = float(np.sqrt(np.sum(p**2)))
    if l2 <= 0:
        return float("nan")
    return float(np.sqrt(2.0 * np.log(2.0) / np.pi) / l2)


def effective_pixel_scale(wcs) -> float | None:
    """Mean pixel scale of a WCS in arcsec, or ``None``."""
    if wcs is None:
        return None
    from astropy.wcs.utils import proj_plane_pixel_scales

    try:
        return float(np.mean(proj_plane_pixel_scales(wcs)) * 3600.0)
    except Exception:  # noqa: BLE001
        return None


def measure_fwhm(
    image: np.ndarray,
    wcs=None,
    threshold_sigma: float = 5.0,
    mask: np.ndarray | None = None,
) -> MeasuredQuality:
    """Measure FWHM / ellipticity / star count from the image itself."""
    catalog = detect_sources(image, threshold_sigma=threshold_sigma, mask=mask)
    scale = effective_pixel_scale(wcs)
    if len(catalog) == 0:
        return MeasuredQuality(None, None, None, 0, catalog.background_rms, scale)
    f = catalog.fwhm
    ok = np.isfinite(f) & (f > 0.5) & (f < 0.25 * min(image.shape))
    fwhm = float(np.median(f[ok])) if ok.any() else float(np.median(f))
    ecc = float(np.median(catalog.eccentricity[ok])) if ok.any() else None
    return MeasuredQuality(
        fwhm_pixels=fwhm,
        fwhm_arcsec=fwhm * scale if scale else None,
        eccentricity=ecc,
        star_count=len(catalog),
        background_rms=catalog.background_rms,
        pixel_scale_arcsec=scale,
    )


def _psf_stamp(psf: np.ndarray, shape: tuple[int, int], y: float, x: float) -> tuple[np.ndarray, tuple[slice, slice]]:
    """Place a PSF at ``(y, x)`` and return the overlapping stamp and slices."""
    k = np.asarray(psf, dtype=np.float64)
    ky, kx = k.shape
    iy, ix = round(y), round(x)
    y0, x0 = iy - ky // 2, ix - kx // 2
    ys, xs = max(y0, 0), max(x0, 0)
    ye, xe = min(y0 + ky, shape[0]), min(x0 + kx, shape[1])
    if ye <= ys or xe <= xs:
        return np.zeros((0, 0)), (slice(0, 0), slice(0, 0))
    stamp = k[ys - y0 : ye - y0, xs - x0 : xe - x0]
    return stamp, (slice(ys, ye), slice(xs, xe))


def matched_filter_snr(
    image: np.ndarray,
    psf: np.ndarray,
    positions: np.ndarray,
    noise: np.ndarray | float,
) -> np.ndarray:
    r"""Detection significance of point sources, each image with its own PSF.

    For an image ``d`` with noise ``sigma`` and unit-sum PSF ``p`` centred on
    the source, the optimal (matched-filter / maximum-likelihood) estimate of
    the source flux and its error give::

        SNR = sum_i p_i d_i / sigma_i**2  /  sqrt( sum_i p_i**2 / sigma_i**2 )

    which reduces to ``sum(p*d) / (sigma * ||p||_2)`` for uniform noise. This
    is the *only* fair way to compare coadds with different effective PSFs.

    Parameters
    ----------
    positions
        ``(M, 2)`` array of ``(y, x)`` pixel positions.
    noise
        Scalar sigma or a per-pixel sigma map.
    """
    d = np.asarray(image, dtype=np.float64)
    pos = np.atleast_2d(np.asarray(positions, dtype=np.float64))
    if np.isscalar(noise) or np.ndim(noise) == 0:
        sigma_map = np.full(d.shape, float(noise), dtype=np.float64)
    else:
        sigma_map = np.asarray(noise, dtype=np.float64)

    out = np.zeros(pos.shape[0], dtype=np.float64)
    for i, (y, x) in enumerate(pos):
        stamp, sl = _psf_stamp(psf, d.shape, y, x)
        if stamp.size == 0:
            out[i] = np.nan
            continue
        s = np.maximum(sigma_map[sl], 1e-12)
        inv_var = 1.0 / (s * s)
        num = float(np.sum(stamp * d[sl] * inv_var))
        den = float(np.sqrt(np.sum(stamp * stamp * inv_var)))
        out[i] = num / den if den > 0 else np.nan
    return out


def snr_gain_db(coadd_snr: np.ndarray | float, baseline_snr: np.ndarray | float) -> float:
    """SNR gain in dB, ``20 log10(coadd / baseline)``, over the median source."""
    a = np.atleast_1d(np.asarray(coadd_snr, dtype=np.float64))
    b = np.atleast_1d(np.asarray(baseline_snr, dtype=np.float64))
    ok = np.isfinite(a) & np.isfinite(b) & (b > 0)
    if not ok.any():
        return float("nan")
    return float(20.0 * np.log10(np.median(a[ok] / b[ok])))


def flux_ratio(before: np.ndarray, after: np.ndarray, mask: np.ndarray | None = None) -> float:
    """Total flux of ``after`` over ``before``. 1.0 means flux conserving."""
    a = np.asarray(before, dtype=np.float64)
    b = np.asarray(after, dtype=np.float64)
    if mask is not None:
        a = a[mask]
        b = b[mask]
    ta = float(np.nansum(a))
    if abs(ta) < 1e-30:
        return float("nan")
    return float(np.nansum(b) / ta)


def depth_curve(
    frames,
    positions: np.ndarray,
    combiner,
    counts: list[int] | None = None,
) -> list[dict[str, float]]:
    """Detection SNR versus number of contributing frames.

    This is the "per-target depth curve" of section 9's *free extras*, and it
    is also the empirical check on the ``sqrt(N)`` scaling that the whole
    product rests on. Frames are taken in sorted order, so the curve is
    reproducible.
    """
    ordered = sorted(frames, key=lambda f: f.frame_id)
    if counts is None:
        counts = sorted({max(1, round(len(ordered) * f)) for f in (0.125, 0.25, 0.5, 1.0)})
    out = []
    for n in counts:
        if n < 1 or n > len(ordered):
            continue
        result = combiner(ordered[:n])
        psf = result.psf
        if psf is None:
            continue
        sigma = result.uncertainty if result.uncertainty is not None else 1.0
        snr = matched_filter_snr(result.image, psf, positions, sigma)
        out.append(
            {
                "n_frames": float(n),
                "median_snr": float(np.nanmedian(snr)),
                "sqrt_n": float(np.sqrt(n)),
            }
        )
    return out
