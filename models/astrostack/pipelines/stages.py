"""Stage implementations and the op registry.

Every ``op:`` name in a pipeline YAML resolves to a function here. The
signature is uniform::

    def stage(ctx: RunContext, inputs: StageInputs, **params) -> Any

``inputs`` wraps the results of the stages this one ``needs``, with
type-directed accessors (``inputs.frames``, ``inputs.grid``, ``inputs.coadd``)
so a YAML author does not have to wire results positionally.

Stages are pure with respect to ``ctx.seed``: any randomness comes from
``astrostack.rng.generator(ctx.seed, stage_id, ...)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from astrostack.align.platesolve import build_solver
from astrostack.align.register import (
    OutputGrid,
    cross_validate_registration,
    make_output_grid,
    reproject_frame,
)
from astrostack.align.stars import characterise_frame
from astrostack.calibrate.background import subtract_background
from astrostack.calibrate.cosmicray import lacosmic
from astrostack.calibrate.masters import MasterFrames, apply_calibration, combine_masters
from astrostack.enhance.deconv import operator_psf, richardson_lucy, wiener_deconvolve
from astrostack.enhance.hdr import hdr_composite
from astrostack.errors import PipelineConfigError
from astrostack.io.frame import Frame
from astrostack.io.loaders import compute_airmass, load_frame
from astrostack.io.manifest import Manifest
from astrostack.io.writers import write_preview_png, write_result_fits
from astrostack.logging import get_logger
from astrostack.metrics.injection import injection_experiment
from astrostack.metrics.quality import effective_pixel_scale, measure_fwhm
from astrostack.pipelines.provenance import ProvenanceRecorder
from astrostack.robust import robust_sigma
from astrostack.stack import combine, drizzle, optimal_coadd
from astrostack.stack.base import CoaddResult

__all__ = ["OPS", "RunContext", "StageInputs", "get_op", "register_op"]

log = get_logger(__name__)

OPS: dict[str, Callable[..., Any]] = {}


def register_op(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in OPS:
            raise ValueError(f"op {name!r} already registered")
        OPS[name] = fn
        return fn

    return wrap


def get_op(name: str) -> Callable[..., Any]:
    if name not in OPS:
        raise PipelineConfigError(f"unknown op {name!r}; known: {sorted(OPS)}")
    return OPS[name]


@dataclass(slots=True)
class RunContext:
    """Everything a stage may read about the run as a whole."""

    seed: int
    out_dir: Path
    manifest: Manifest
    provenance: ProvenanceRecorder
    pipeline: str
    stage_id: str = ""
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StageInputs:
    """Results of the upstream stages, keyed by stage id."""

    values: dict[str, Any]

    def get(self, stage_id: str) -> Any:
        return self.values[stage_id]

    def _first(self, predicate: Callable[[Any], bool], what: str) -> Any:
        for key in sorted(self.values):
            v = self.values[key]
            if predicate(v):
                return v
        raise PipelineConfigError(
            f"this stage needs {what} but none of its dependencies "
            f"({sorted(self.values)}) produced one"
        )

    @property
    def frames(self) -> list[Frame]:
        return self._first(
            lambda v: isinstance(v, list) and v and isinstance(v[0], Frame), "a list of frames"
        )

    @property
    def grid(self) -> OutputGrid:
        return self._first(lambda v: isinstance(v, OutputGrid), "an output grid")

    @property
    def coadd(self) -> CoaddResult:
        return self._first(lambda v: isinstance(v, CoaddResult), "a coadd")

    def optional_coadd(self) -> CoaddResult | None:
        try:
            return self.coadd
        except PipelineConfigError:
            return None


def _sorted(frames: list[Frame]) -> list[Frame]:
    """Deterministic input order. Never trust the caller."""
    return sorted(frames, key=lambda f: f.frame_id)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
@register_op("io.load")
def op_load(
    ctx: RunContext,
    inputs: StageInputs,
    channel: str = "G",
    fits_plane: int | None = None,
    demosaic_raw: bool = False,
    estimate_jpeg_gamma: bool = True,
    max_frames: int | None = None,
) -> list[Frame]:
    """Load every manifest entry into a linear :class:`Frame`."""
    specs = ctx.manifest.inputs
    if max_frames:
        specs = specs[: int(max_frames)]
    frames: list[Frame] = []
    for spec in specs:
        frame = load_frame(
            spec.path,
            meta=spec.meta,
            channel=channel,
            fits_plane=fits_plane,
            demosaic_raw=demosaic_raw,
            estimate_jpeg_gamma=estimate_jpeg_gamma,
        )
        frames.append(frame)
        ctx.provenance.add_input(
            photo_id=frame.meta.photo_id,
            path=spec.path,
            license_code=frame.meta.license,
            attribution=frame.meta.attribution_name or frame.meta.owner_display_name,
            data_sha256=frame.checksum(),
            extra={
                "source_format": frame.meta.source_format,
                "photometrically_unreliable": frame.meta.photometrically_unreliable,
                "shape": list(frame.shape),
            },
        )
    for row in ctx.manifest.rejected:
        ctx.provenance.add_rejected(row["photo_id"], row["reason"])
    return _sorted(frames)


@register_op("io.frames")
def op_frames(ctx: RunContext, inputs: StageInputs) -> list[Frame]:
    """Pass frames injected directly into ``ctx.scratch['frames']``.

    Used by the training and evaluation harnesses, which build frames in
    memory rather than from disk.
    """
    frames = ctx.scratch.get("frames")
    if not frames:
        raise PipelineConfigError("op io.frames requires ctx.scratch['frames']")
    return _sorted(list(frames))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
@register_op("calibrate.masters")
def op_masters(
    ctx: RunContext,
    inputs: StageInputs,
    bias_dir: str | None = None,
    dark_dir: str | None = None,
    flat_dir: str | None = None,
    dark_exposure_s: float | None = None,
    flat_floor: float = 0.2,
) -> list[Frame]:
    """Apply bias/dark/flat when the contributor supplied calibration frames."""
    frames = inputs.frames

    def _stack(directory: str | None) -> np.ndarray | None:
        if not directory:
            return None
        root = Path(directory)
        if not root.is_dir():
            log.warning("calibration_dir_missing", directory=str(root))
            return None
        files = sorted(p for p in root.iterdir() if p.is_file() and not p.name.startswith("."))
        if not files:
            return None
        arrays = [load_frame(p).data for p in files]
        return np.stack(arrays)

    masters = combine_masters(
        bias_stack=_stack(bias_dir),
        dark_stack=_stack(dark_dir),
        flat_stack=_stack(flat_dir),
        dark_exposure_s=dark_exposure_s,
    )
    if masters == MasterFrames(dark_exposure_s=dark_exposure_s):
        for f in frames:
            f.note("calibrate.masters", "no calibration frames supplied", flux_preserving=True)
        return frames
    return _sorted([apply_calibration(f, masters, flat_floor=flat_floor) for f in frames])


@register_op("calibrate.cosmic_rays")
def op_cosmic_rays(
    ctx: RunContext,
    inputs: StageInputs,
    sigclip: float = 4.5,
    objlim: float = 5.0,
    max_iter: int = 4,
    enabled: bool = True,
) -> list[Frame]:
    """L.A.Cosmic. Flags, never interpolates: another frame will cover it."""
    frames = inputs.frames
    if not enabled:
        return frames
    out = []
    for f in frames:
        res = lacosmic(
            f.data,
            gain_e_per_adu=f.meta.gain_e_per_adu or 1.0,
            read_noise_e=f.meta.read_noise_e or 5.0,
            sigclip=sigclip,
            objlim=objlim,
            max_iter=max_iter,
            background=f.background,
        )
        mask = res.mask if f.mask is None else (f.mask | res.mask)
        new = f.copy_with(f.data, mask=mask)
        new.note(
            "calibrate.cosmic_rays",
            f"L.A.Cosmic flagged {res.n_flagged} px ({res.fraction():.4%}) in {res.iterations} iters",
            flux_preserving=True,
        )
        out.append(new)
    return _sorted(out)


@register_op("calibrate.background")
def op_background(
    ctx: RunContext,
    inputs: StageInputs,
    box_size: int | None = None,
    filter_size: int = 3,
    sigma: float = 3.0,
    keep_pedestal: bool = False,
) -> list[Frame]:
    """Fit and remove the spatially varying sky (light pollution)."""
    return _sorted(
        [
            subtract_background(
                f, box_size=box_size, filter_size=filter_size, sigma=sigma,
                keep_pedestal=keep_pedestal,
            )
            for f in inputs.frames
        ]
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Astrometry and characterisation
# ---------------------------------------------------------------------------
@register_op("align.platesolve")
def op_platesolve(
    ctx: RunContext,
    inputs: StageInputs,
    solver: str = "noop",
    solver_options: dict[str, Any] | None = None,
    scale_tolerance: float = 0.25,
    on_failure: str = "reject",
) -> list[Frame]:
    """Plate solve every frame, seeded by the EXIF pixel-scale prior."""
    impl = build_solver(solver, **(solver_options or {}))
    out: list[Frame] = []
    for f in inputs.frames:
        try:
            out.append(impl.solve_frame(f, tolerance=scale_tolerance))
        except Exception as exc:  # noqa: BLE001 - solver failure is a normal outcome
            if on_failure == "keep":
                f.note("align.platesolve", f"unsolved ({exc}); kept", flux_preserving=True)
                out.append(f)
                continue
            log.warning("platesolve_failed", frame=f.frame_id, error=str(exc))
            ctx.provenance.add_rejected(f.meta.photo_id, f"plate solving failed: {exc}")
    if not out:
        raise PipelineConfigError("no frame could be plate solved")
    return _sorted(out)


@register_op("align.characterise")
def op_characterise(
    ctx: RunContext,
    inputs: StageInputs,
    threshold_sigma: float = 5.0,
    min_area: int = 5,
    field_grid: list[int] | tuple[int, int] = (3, 3),
    epsf_size: int = 25,
    psf_model: str = "moffat",
    compute_airmass_from_wcs: bool = True,
) -> list[Frame]:
    """Measure sources, PSF, background statistics and airmass."""
    grid = (int(field_grid[0]), int(field_grid[1]))
    out = []
    for f in inputs.frames:
        g = characterise_frame(
            f,
            threshold_sigma=threshold_sigma,
            min_area=min_area,
            field_grid=grid,
            epsf_size=epsf_size,
            psf_model=psf_model,
        )
        if compute_airmass_from_wcs and g.wcs is not None:
            h, w = g.shape
            centre = g.wcs.pixel_to_world(w / 2.0, h / 2.0)
            airmass, alt, parallactic = compute_airmass(
                float(centre.ra.deg),
                float(centre.dec.deg),
                g.meta.captured_at_utc,
                g.meta.latitude_deg,
                g.meta.longitude_deg,
                g.meta.elevation_m,
            )
            g.quality = g.quality.model_copy(
                update={
                    "airmass": airmass,
                    "parallactic_angle_deg": parallactic,
                }
            )
            g.extra["altitude_deg"] = alt
        out.append(g)

    sources = {f.extra.get("psf_source") for f in out}
    if len(sources) > 1:
        # A corpus characterised two different ways is worse than one
        # characterised consistently, however good the better method is.
        ctx.provenance.warn(
            f"PSF measured inconsistently across the corpus ({sorted(map(str, sources))}); "
            "set psf_model to moffat or gaussian for a uniform characterisation"
        )
        log.warning("mixed_psf_sources", sources=sorted(map(str, sources)))
    return _sorted(out)


@register_op("align.output_grid")
def op_output_grid(
    ctx: RunContext,
    inputs: StageInputs,
    pixel_scale_arcsec: float | None = None,
    scale_percentile: float = 25.0,
    max_oversample: float = 2.0,
    pad_fraction: float = 0.02,
) -> OutputGrid:
    """Choose the common tangent plane, honouring the measured dither diversity."""
    grid = make_output_grid(
        inputs.frames,
        pixel_scale_arcsec=pixel_scale_arcsec,
        scale_percentile=scale_percentile,
        max_oversample=max_oversample,
        pad_fraction=pad_fraction,
    )
    if grid.oversample > 1.0 and grid.dither_score < 0.4:  # pragma: no cover - guarded upstream
        ctx.provenance.warn("output grid oversampled without sufficient dither diversity")
    log.info(
        "output_grid",
        shape=grid.shape,
        scale=round(grid.pixel_scale_arcsec, 4),
        oversample=grid.oversample,
        dither_score=round(grid.dither_score, 3),
    )
    return grid


@register_op("align.register")
def op_register(
    ctx: RunContext,
    inputs: StageInputs,
    method: str = "adaptive",
    conserve_flux: bool = True,
    kernel: str = "gaussian",
    cross_validate: bool = True,
    max_rms_px: float = 1.5,
) -> list[Frame]:
    """Reproject frames onto the common grid, with optional cross-validation."""
    frames = inputs.frames
    grid = inputs.grid
    reference = frames[0]

    kept: list[Frame] = []
    for f in frames:
        if cross_validate and f is not reference and f.wcs is not None:
            report = cross_validate_registration(
                f, reference, f.wcs, max_rms_px=max_rms_px
            )
            f.extra["registration"] = report.as_dict()
            if not report.accepted:
                ctx.provenance.add_rejected(f.meta.photo_id, report.reason)
                log.warning("registration_rejected", frame=f.frame_id, reason=report.reason)
                continue
        kept.append(
            reproject_frame(
                f, grid, method=method, conserve_flux=conserve_flux, kernel=kernel
            )
        )
    if not kept:
        raise PipelineConfigError("every frame failed registration cross-validation")
    return _sorted(kept)


# ---------------------------------------------------------------------------
# Coaddition
# ---------------------------------------------------------------------------
@register_op("stack.simple")
def op_stack_simple(
    ctx: RunContext,
    inputs: StageInputs,
    method: str = "sigma-clip",
    weighting: str = "inverse-variance",
    sigma_low: float = 5.0,
    sigma_high: float = 3.0,
    iterations: int = 3,
    reject_trails: bool = True,
) -> CoaddResult:
    """The honest baseline."""
    result = combine(
        inputs.frames,
        method=method,
        weighting=weighting,
        sigma_low=sigma_low,
        sigma_high=sigma_high,
        iterations=iterations,
        reject_trails=reject_trails,
    )
    ctx.provenance.set_weights(result.frame_weights)
    return result


@register_op("stack.optimal")
def op_stack_optimal(
    ctx: RunContext,
    inputs: StageInputs,
    epsilon: float = 1e-4,
    reject: bool = True,
    sigma_low: float = 5.0,
    sigma_high: float = 3.0,
    reject_trails: bool = True,
    psf_output_size: int = 33,
) -> CoaddResult:
    """Zackay & Ofek proper coaddition."""
    result = optimal_coadd(
        inputs.frames,
        epsilon=epsilon,
        reject=reject,
        sigma_low=sigma_low,
        sigma_high=sigma_high,
        reject_trails=reject_trails,
        psf_output_size=psf_output_size,
    )
    ctx.provenance.set_weights(result.frame_weights)
    return result


@register_op("stack.drizzle")
def op_stack_drizzle(
    ctx: RunContext,
    inputs: StageInputs,
    pixfrac: float = 0.7,
    subsample: int = 5,
    weighting: str = "uniform",
) -> CoaddResult:
    """Fruchter & Hook drizzle straight from the native grids."""
    result = drizzle(
        inputs.frames, inputs.grid, pixfrac=pixfrac, subsample=subsample, weighting=weighting
    )
    ctx.provenance.set_weights(result.frame_weights)
    return result


@register_op("enhance.hdr")
def op_hdr(ctx: RunContext, inputs: StageInputs, knee: float = 0.75) -> CoaddResult:
    """Composite the exposure spread into one linear high-dynamic-range image."""
    return hdr_composite(inputs.frames, knee=knee).coadd


@register_op("enhance.deconvolve")
def op_deconvolve(
    ctx: RunContext,
    inputs: StageInputs,
    method: str = "richardson-lucy",
    iterations: int = 15,
    damping: float = 0.0,
    max_iterations: int = 60,
    nsr: float = 1e-3,
    enabled: bool = True,
) -> CoaddResult:
    """Bounded deconvolution of the coadd with its own effective PSF."""
    coadd = inputs.coadd
    if not enabled or coadd.psf is None:
        return coadd

    diffraction_px = None
    frames = None
    try:
        frames = inputs.frames
    except PipelineConfigError:
        frames = None
    if frames:
        limits = [
            f.meta.diffraction_limit_arcsec()
            for f in frames
            if f.meta.diffraction_limit_arcsec() is not None
        ]
        scale = effective_pixel_scale(coadd.wcs)
        if limits and scale:
            # The best contributing optic sets the wall (section 5).
            diffraction_px = float(min(limits) / scale)

    if method == "richardson-lucy":
        res = richardson_lucy(
            coadd.image, coadd.psf, iterations=iterations, damping=damping,
            max_iterations=max_iterations, diffraction_limit_pixels=diffraction_px,
        )  # fmt: skip
        post_psf = operator_psf(
            coadd.psf, "richardson-lucy", iterations=res.iterations, damping=damping
        )
    elif method == "wiener":
        sigma = (
            float(np.median(coadd.uncertainty[coadd.uncertainty > 0]))
            if coadd.uncertainty is not None and np.any(coadd.uncertainty > 0)
            else None
        )
        res = wiener_deconvolve(
            coadd.image, coadd.psf, nsr=nsr, noise_sigma=sigma,
            diffraction_limit_pixels=diffraction_px,
        )  # fmt: skip
        post_psf = operator_psf(coadd.psf, "wiener", nsr=nsr)
    else:
        raise PipelineConfigError(f"unknown deconvolution method {method!r}")

    for warning in res.warnings:
        ctx.provenance.warn(f"deconvolution: {warning}")

    # The deconvolved image no longer has the PSF it started with. Publishing
    # the old one would make every downstream matched filter wrong.
    out = CoaddResult(
        image=res.image,
        weight=coadd.weight,
        uncertainty=coadd.uncertainty,
        psf=post_psf,
        wcs=coadd.wcs,
        method=f"{coadd.method}+{res.method}",
        n_frames=coadd.n_frames,
        flux_preserving=coadd.flux_preserving and res.flux_preserving,
        frame_weights=coadd.frame_weights,
        rejected_fraction=coadd.rejected_fraction,
        metrics={**coadd.metrics, "deconvolution": res.summary()},
        notes=[*coadd.notes, *res.warnings],
    )
    return out


# ---------------------------------------------------------------------------
# Tier B
# ---------------------------------------------------------------------------
@register_op("sr.enhance")
def op_sr_enhance(
    ctx: RunContext,
    inputs: StageInputs,
    architecture: str = "wcs-burst",
    weights: str | None = None,
    scale: float = 2.0,
    device: str | None = None,
    allow_untrained: bool = False,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Optional Tier B enhancement layer over the Tier A coadd.

    Returns a dict rather than a ``CoaddResult`` so that no downstream stage
    can mistake a learned image for a measurement.
    """
    from astrostack.sr.base import SRInputs, build_resolver
    from astrostack.sr.uncertainty import prior_contribution

    coadd = inputs.coadd
    resolver = build_resolver(
        architecture, scale=scale, device=device, weights=weights,
        allow_untrained=allow_untrained, **(options or {}),
    )  # fmt: skip
    sr_inputs = SRInputs.from_frames(inputs.frames, reference_coadd=coadd, output_grid=inputs.grid)
    result = resolver.enhance(sr_inputs)

    if coadd.psf is not None:
        try:
            result.prior_contribution = prior_contribution(result.image, coadd.image, coadd.psf)
        except ValueError as exc:  # pragma: no cover
            log.warning("prior_contribution_failed", error=str(exc))
    ctx.provenance.warn(f"Tier B layer present: {result.label}")
    return {"sr": result}


# ---------------------------------------------------------------------------
# Measurement and delivery
# ---------------------------------------------------------------------------
@register_op("metrics.evaluate")
def op_metrics(
    ctx: RunContext,
    inputs: StageInputs,
    baseline_method: str = "sigma-clip",
    threshold_sigma: float = 5.0,
) -> dict[str, Any]:
    """Measure the coadd and compare it against the honest baseline."""
    from astrostack.metrics.quality import matched_filter_snr, snr_gain_db

    coadd = inputs.coadd
    frames = inputs.frames
    measured = measure_fwhm(coadd.image, coadd.wcs, threshold_sigma=threshold_sigma)

    out: dict[str, Any] = {
        "coadd": measured.as_dict(),
        "method": coadd.method,
        "n_frames": coadd.n_frames,
        "flux_preserving": coadd.flux_preserving,
        "effective_pixel_scale": effective_pixel_scale(coadd.wcs),
    }

    input_fwhm = [f.quality.fwhm_arcsec for f in frames if f.quality.fwhm_arcsec]
    if input_fwhm:
        out["input_fwhm_arcsec"] = {
            "best": float(np.min(input_fwhm)),
            "median": float(np.median(input_fwhm)),
            "worst": float(np.max(input_fwhm)),
        }

    if coadd.method != f"simple:{baseline_method}" and coadd.psf is not None:
        baseline = combine(frames, method=baseline_method, weighting="inverse-variance")
        from astrostack.align.stars import detect_sources

        cat = detect_sources(coadd.image, threshold_sigma=max(threshold_sigma, 8.0))
        if len(cat) >= 3 and baseline.psf is not None:
            pos = np.column_stack([cat.y, cat.x])[:50]
            # Both images are filtered with their OWN effective PSF and their
            # OWN empirically measured background sigma. Using each result's
            # propagated uncertainty map instead would compare two different
            # noise *conventions* rather than two coadds, and the winner would
            # be decided by bookkeeping.
            noise_c = robust_sigma(coadd.image, mask=(coadd.weight <= 0))
            noise_b = robust_sigma(baseline.image, mask=(baseline.weight <= 0))
            snr_c = matched_filter_snr(coadd.image, coadd.psf, pos, noise_c)
            snr_b = matched_filter_snr(baseline.image, baseline.psf, pos, noise_b)
            out["baseline"] = {
                "method": baseline.method,
                "median_snr": float(np.nanmedian(snr_b)),
                "measured_sigma": float(noise_b),
            }
            out["median_snr"] = float(np.nanmedian(snr_c))
            out["measured_sigma"] = float(noise_c)
            out["snr_gain_db"] = snr_gain_db(snr_c, snr_b)
    ctx.provenance.metrics.update(out)
    return out


@register_op("metrics.injection_audit")
def op_injection_audit(
    ctx: RunContext,
    inputs: StageInputs,
    n_sources: int = 25,
    flux_levels: list[float] | None = None,
    detection_threshold: float = 5.0,
    combiner: str = "optimal",
    enabled: bool = True,
) -> dict[str, Any]:
    """Inject sources of known flux and measure the recovery curve.

    This is the audit that rule 4 requires. It runs on the *registered*
    frames, so it audits the whole coaddition path rather than a toy.
    """
    if not enabled:
        return {"enabled": False}
    frames = inputs.frames
    sigma = float(np.median([f.quality.noise_sigma or 1.0 for f in frames]))
    fluxes = np.asarray(
        flux_levels if flux_levels else [5.0 * sigma, 15.0 * sigma, 50.0 * sigma, 150.0 * sigma],
        dtype=np.float64,
    )

    def _combine(fs: list[Frame]) -> CoaddResult:
        if combiner == "optimal":
            return optimal_coadd(fs)
        return combine(fs, method="sigma-clip", weighting="inverse-variance")

    report = injection_experiment(
        frames,
        _combine,
        fluxes=fluxes,
        n_sources=n_sources,
        seed=ctx.seed,
        detection_threshold=detection_threshold,
    )
    for note in report.notes:
        ctx.provenance.warn(f"injection audit: {note}")
    payload = report.as_dict()
    ctx.provenance.metrics["injection_audit"] = {
        k: v for k, v in payload.items() if k != "curve"
    }
    return payload


@register_op("io.write")
def op_write(
    ctx: RunContext,
    inputs: StageInputs,
    fits_name: str = "coadd.fits",
    preview_name: str = "preview.png",
    write_preview: bool = True,
    output_license: str | None = None,
) -> dict[str, Any]:
    """Write the deliverables: FITS, preview, provenance, attribution."""
    coadd = inputs.coadd
    out_dir = ctx.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    sr_result = None
    for value in inputs.values.values():
        if isinstance(value, dict) and "sr" in value:
            sr_result = value["sr"]

    header_cards = {
        "PIPELINE": ctx.pipeline,
        "STACKMTH": coadd.method,
        "NFRAMES": coadd.n_frames,
        "FLUXCONS": bool(coadd.flux_preserving),
        "SEED": ctx.seed,
    }
    fits_path = out_dir / fits_name
    checksum = write_result_fits(
        fits_path,
        image=coadd.image,
        weight=coadd.weight,
        uncertainty=coadd.uncertainty,
        psf=coadd.psf,
        wcs=coadd.wcs,
        header_cards=header_cards,
        history=coadd.notes,
    )
    ctx.provenance.add_output(
        "fits", fits_path, checksum, method=coadd.method, shape=list(coadd.image.shape)
    )

    # Relative names only: an absolute path would carry the output directory
    # into the run checksum and make two runs of the same job differ.
    outputs: dict[str, Any] = {"fits": fits_name, "fits_sha256": checksum}

    if write_preview:
        preview_path = out_dir / preview_name
        write_preview_png(preview_path, coadd.image)
        from astrostack.pipelines.provenance import file_sha256

        ctx.provenance.add_output("preview", preview_path, file_sha256(preview_path))
        outputs["preview"] = preview_name

    if sr_result is not None:
        sr_path = out_dir / "enhanced.fits"
        sr_checksum = write_result_fits(
            sr_path,
            image=sr_result.image,
            uncertainty=sr_result.uncertainty,
            wcs=coadd.wcs,
            header_cards={
                "PIPELINE": ctx.pipeline,
                "TIER": "B",
                "ARCH": sr_result.architecture,
                "AIGEN": True,
                "PRIORFRC": sr_result.prior_contribution,
            },
            history=[sr_result.label, *sr_result.notes],
        )
        ctx.provenance.add_output("enhanced_fits", sr_path, sr_checksum, **sr_result.summary())
        outputs["enhanced_fits"] = sr_path.name

    from astrostack.pipelines.provenance import write_attribution

    attribution_path = out_dir / "ATTRIBUTION.md"
    write_attribution(
        attribution_path,
        ctx.pipeline,
        ctx.manifest.attribution_rows(),
        weights=coadd.frame_weights,
        output_license=output_license,
    )
    outputs["attribution"] = attribution_path.name
    return outputs
