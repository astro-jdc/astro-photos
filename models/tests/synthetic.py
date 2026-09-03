"""Synthetic star-field generator with known truth.

No binary fixtures are committed to the repository. Every test builds its own
data here, from a seed, so the truth is exactly known and the tests stay
readable and diffable.

What the generator models, and why each piece is there:

* **Positions and fluxes** drawn from a power-law luminosity function, so the
  field has the realistic long tail of faint sources that dominates any
  completeness measurement.
* **Per-frame PSF** — a Moffat or Gaussian of specified FWHM, so a "corpus"
  can be built with seeing varying by a factor of several, which is the
  regime where Zackay-Ofek coaddition earns its keep.
* **Sub-pixel dither**, controlled and known. This is the ingredient drizzle
  and multi-frame SR need; a test that omits it would be testing
  interpolation.
* **Poisson shot noise plus Gaussian read noise**, on top of a sky background
  that can carry a **gradient** (light pollution).
* **A real WCS** on every frame, offset by the dither, so the WCS-driven
  registration path is exercised for real rather than stubbed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from astrostack.align.platesolve import make_tangent_wcs
from astrostack.align.stars import gaussian_kernel, moffat_kernel
from astrostack.io.frame import Frame, FrameMetadata, FrameQuality, PSFModel
from astrostack.rng import generator

__all__ = ["FieldTruth", "SyntheticField", "make_corpus", "subtract_known_sky", "write_corpus"]


@dataclass(slots=True)
class FieldTruth:
    """Ground truth of a synthetic field, in reference-grid pixel units."""

    x: np.ndarray
    y: np.ndarray
    flux: np.ndarray
    shape: tuple[int, int]
    pixel_scale_arcsec: float
    ra_deg: float
    dec_deg: float

    @property
    def positions(self) -> np.ndarray:
        """``(N, 2)`` array of ``(y, x)``."""
        return np.column_stack([self.y, self.x])

    def brightest(self, n: int) -> np.ndarray:
        order = np.argsort(-self.flux)[:n]
        return np.column_stack([self.y[order], self.x[order]])


@dataclass(slots=True)
class SyntheticField:
    """A generated corpus: frames plus the truth they were drawn from."""

    frames: list[Frame]
    truth: FieldTruth
    reference_wcs: object = None
    dither: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))


def _render(
    shape: tuple[int, int],
    xs: np.ndarray,
    ys: np.ndarray,
    fluxes: np.ndarray,
    kernel: np.ndarray,
) -> np.ndarray:
    """Render point sources at sub-pixel positions via Fourier phase shift."""
    h, w = shape
    scene = np.zeros((h, w), dtype=np.float64)
    ky, kx = kernel.shape
    ku = np.fft.fft2(kernel)
    v = np.fft.fftfreq(ky)[:, None]
    u = np.fft.fftfreq(kx)[None, :]

    for x, y, f in zip(xs, ys, fluxes, strict=True):
        ix, iy = int(np.floor(x)), int(np.floor(y))
        fx, fy = float(x - ix), float(y - iy)
        shifted = np.real(np.fft.ifft2(ku * np.exp(-2j * np.pi * (v * fy + u * fx))))
        shifted = np.clip(shifted, 0.0, None)
        s = shifted.sum()
        if s <= 0:
            continue
        shifted = shifted / s * float(f)
        y0, x0 = iy - ky // 2, ix - kx // 2
        ys0, xs0 = max(y0, 0), max(x0, 0)
        ye, xe = min(y0 + ky, h), min(x0 + kx, w)
        if ye <= ys0 or xe <= xs0:
            continue
        scene[ys0:ye, xs0:xe] += shifted[ys0 - y0 : ye - y0, xs0 - x0 : xe - x0]
    return scene


def make_corpus(
    n_frames: int = 8,
    shape: tuple[int, int] = (128, 128),
    n_stars: int = 40,
    seed: int = 20240101,
    fwhm_pixels: float | list[float] = 3.0,
    sky_level: float | list[float] = 200.0,
    read_noise: float = 5.0,
    gain: float = 1.0,
    flux_range: tuple[float, float] = (300.0, 30000.0),
    dither_pixels: float = 1.5,
    sky_gradient: float = 0.0,
    pixel_scale_arcsec: float = 2.0,
    ra_deg: float = 83.822,
    dec_deg: float = -5.391,
    psf_shape: str = "moffat",
    add_noise: bool = True,
    with_wcs: bool = True,
    attach_truth_psf: bool = True,
    n_cosmic_rays: int = 0,
    trail_frames: tuple[int, ...] = (),
    empty: bool = False,
) -> SyntheticField:
    """Generate ``n_frames`` synthetic exposures of the same field.

    ``fwhm_pixels`` and ``sky_level`` accept a list to build a *heterogeneous*
    corpus (good frames and bad frames), which is what the optimal-coaddition
    test needs.

    ``attach_truth_psf`` puts the exact rendering kernel on each frame. That is
    deliberate for algorithm tests: it isolates the coaddition mathematics from
    PSF-measurement error. Tests that exercise the measurement path call
    ``characterise_frame`` instead.
    """
    h, w = shape
    rng = generator(seed, "field")

    if empty:
        xs = np.empty(0)
        ys = np.empty(0)
        fluxes = np.empty(0)
    else:
        margin = 8
        xs = rng.uniform(margin, w - margin, n_stars)
        ys = rng.uniform(margin, h - margin, n_stars)
        # Power-law luminosity function: many faint, few bright.
        u = rng.uniform(0.0, 1.0, n_stars)
        lo, hi = flux_range
        alpha = 1.5
        fluxes = (lo ** (1 - alpha) + u * (hi ** (1 - alpha) - lo ** (1 - alpha))) ** (1 / (1 - alpha))

    fwhms = list(fwhm_pixels) if isinstance(fwhm_pixels, list | tuple) else [float(fwhm_pixels)] * n_frames
    skies = list(sky_level) if isinstance(sky_level, list | tuple) else [float(sky_level)] * n_frames
    fwhms = [fwhms[i % len(fwhms)] for i in range(n_frames)]
    skies = [skies[i % len(skies)] for i in range(n_frames)]

    ref_wcs = make_tangent_wcs(ra_deg, dec_deg, pixel_scale_arcsec, shape) if with_wcs else None
    dither = np.zeros((n_frames, 2), dtype=np.float64)

    frames: list[Frame] = []
    for i in range(n_frames):
        frng = generator(seed, "frame", f"{i:03d}")
        dx = float(frng.uniform(-dither_pixels, dither_pixels)) if dither_pixels > 0 else 0.0
        dy = float(frng.uniform(-dither_pixels, dither_pixels)) if dither_pixels > 0 else 0.0
        dither[i] = (dy, dx)

        kernel = (
            moffat_kernel(fwhms[i], size=25)
            if psf_shape == "moffat"
            else gaussian_kernel(fwhms[i], size=25)
        )
        scene = _render(shape, xs + dx, ys + dy, fluxes, kernel) if xs.size else np.zeros(shape)

        sky = np.full(shape, skies[i], dtype=np.float64)
        if sky_gradient:
            gy, gx = np.mgrid[0:h, 0:w]
            sky += sky_gradient * skies[i] * (gx / max(w - 1, 1) + 0.5 * gy / max(h - 1, 1))

        expected_e = np.clip(scene + sky, 0.0, None) * gain
        if add_noise:
            counts = frng.poisson(expected_e) / gain
            counts = counts + frng.normal(0.0, read_noise / gain, shape)
        else:
            # Noiseless truth: used by the flux-conservation tests, where a
            # noise realisation of a few thousand ADU would swamp the very
            # ratio being measured.
            counts = expected_e / gain

        if n_cosmic_rays:
            for _ in range(int(n_cosmic_rays)):
                cy = int(frng.integers(4, h - 4))
                cx = int(frng.integers(4, w - 4))
                counts[cy, cx] += float(frng.uniform(20, 60)) * max(read_noise, 1.0)

        if i in trail_frames:
            t = np.linspace(0.0, 1.0, max(h, w) * 2)
            y0 = float(frng.uniform(0.15, 0.85)) * h
            x0 = 0.0
            slope = float(frng.uniform(-0.4, 0.4))
            ty = np.clip(y0 + slope * t * w, 0, h - 1).astype(int)
            tx = np.clip(x0 + t * w, 0, w - 1).astype(int)
            counts[ty, tx] += 30.0 * max(read_noise, 1.0)
            counts[np.clip(ty + 1, 0, h - 1), tx] += 15.0 * max(read_noise, 1.0)

        wcs = None
        if with_wcs:
            # The dither is a real pointing offset: shift CRPIX by (-dx, -dy)
            # so that sky position of star k is identical in every frame.
            wcs = make_tangent_wcs(ra_deg, dec_deg, pixel_scale_arcsec, shape)
            wcs.wcs.crpix = [wcs.wcs.crpix[0] + dx, wcs.wcs.crpix[1] + dy]

        meta = FrameMetadata(
            photo_id=f"synthetic-{i:03d}",
            source_format="synthetic",
            exposure_seconds=60.0,
            gain_e_per_adu=gain,
            read_noise_e=read_noise,
            channel="L",
            focal_length_mm=400.0,
            focal_ratio=5.0,
            pixel_pitch_um=3.88,
            license="CC-BY-4.0",
            attribution_name=f"observer-{i:03d}",
        )
        sigma = float(np.sqrt(max(skies[i] / gain, 0.0) + (read_noise / gain) ** 2))
        quality = FrameQuality(
            pixel_scale_arcsec=pixel_scale_arcsec,
            noise_sigma=sigma,
            background_rms=sigma,
            background_adu=skies[i],
            transparency=1.0,
            fwhm_pixels=fwhms[i],
            fwhm_arcsec=fwhms[i] * pixel_scale_arcsec,
            is_plate_solved=with_wcs,
            solver="synthetic" if with_wcs else None,
        )
        variance = (np.clip(counts, 0.0, None) / gain + (read_noise / gain) ** 2).astype(np.float32)
        frame = Frame(
            frame_id=meta.photo_id,
            data=counts.astype(np.float32),
            meta=meta,
            quality=quality,
            variance=variance,
            wcs=wcs,
        )
        if attach_truth_psf:
            frame.psf = PSFModel(
                kernel=kernel.astype(np.float32),
                pixel_scale_arcsec=pixel_scale_arcsec,
                fwhm_pixels=fwhms[i],
                eccentricity=0.0,
                theta_deg=0.0,
                n_stars=int(xs.size),
            )
        frames.append(frame)

    frames.sort(key=lambda f: f.frame_id)
    truth = FieldTruth(
        x=xs, y=ys, flux=fluxes, shape=shape,
        pixel_scale_arcsec=pixel_scale_arcsec, ra_deg=ra_deg, dec_deg=dec_deg,
    )  # fmt: skip
    return SyntheticField(frames=frames, truth=truth, reference_wcs=ref_wcs, dither=dither)


def subtract_known_sky(field: SyntheticField, skies: list[float] | None = None) -> list[Frame]:
    """Return frames with their (known) sky level removed.

    Convenience for tests of the coaddition mathematics, which assume
    background-subtracted input. Real pipelines use
    :func:`astrostack.calibrate.background.subtract_background`.
    """
    out = []
    for i, fr in enumerate(field.frames):
        level = skies[i] if skies else float(fr.quality.background_adu or 0.0)
        new = fr.copy_with((fr.data.astype(np.float64) - level).astype(np.float32))
        out.append(new)
    return out


def write_corpus(
    field: SyntheticField,
    directory,
    manifest_name: str = "manifest.json",
    license_code: str = "CC-BY-4.0",
) -> tuple[str, str]:
    """Write a corpus to disk as FITS + ``manifest.json``.

    Returns ``(directory, manifest_path)``. Used by the CLI and pipeline
    tests, which must exercise the real ingest path rather than hand the
    runner in-memory arrays.
    """
    import json
    from pathlib import Path

    from astropy.io import fits

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for fr in field.frames:
        path = root / f"{fr.frame_id}.fits"
        header = fits.Header()
        if fr.wcs is not None:
            header.update(fr.wcs.to_header(relax=True))
        header["EXPTIME"] = float(fr.meta.exposure_seconds or 60.0)
        header["GAIN"] = float(fr.meta.gain_e_per_adu or 1.0)
        header["RDNOISE"] = float(fr.meta.read_noise_e or 5.0)
        header["FILTER"] = fr.meta.channel or "L"
        fits.PrimaryHDU(data=fr.data.astype(np.float32), header=header).writeto(
            path, overwrite=True
        )
        entries.append(
            {
                "path": path.name,
                "photo_id": fr.meta.photo_id,
                "license": license_code,
                "attribution_name": fr.meta.attribution_name,
                "exposure_seconds": fr.meta.exposure_seconds,
                "gain_e_per_adu": fr.meta.gain_e_per_adu,
                "read_noise_e": fr.meta.read_noise_e,
                "focal_length_mm": fr.meta.focal_length_mm,
                "focal_ratio": fr.meta.focal_ratio,
                "pixel_pitch_um": fr.meta.pixel_pitch_um,
                "channel": fr.meta.channel,
                "allow_ai_training": True,
                "allow_derivatives_in_stacks": True,
            }
        )
    manifest_path = root / manifest_name
    manifest_path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    return str(root), str(manifest_path)
