"""``astrostack`` command line.

Three commands, all of which run offline, on CPU, with no AWS::

    astrostack run <config.yaml> --inputs <dir|manifest.json> --out <dir>
    astrostack inspect <image>          # metadata, astrometry, quality
    astrostack metrics <a> <b>          # comparison of two images

Plus two utilities that fall out of the same machinery: ``ops`` lists the
stage vocabulary a pipeline YAML may use, and ``validate`` type-checks a
config without running it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
import numpy as np

from astrostack.logging import configure, get_logger
from astrostack.version import PIPELINE_API_VERSION, __version__, resolve_git_sha

log = get_logger("astrostack.cli")


def _coerce(raw: str) -> Any:
    """Parse a ``--set`` value as JSON, falling back to the raw string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _echo_json(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="astrostack")
@click.option("--log-level", default="INFO", show_default=True, help="DEBUG/INFO/WARNING/ERROR.")
@click.option("--json-logs", is_flag=True, help="Emit JSON logs (for CloudWatch / AWS Batch).")
def main(log_level: str, json_logs: bool) -> None:
    """Multi-observer astronomical image reconstruction.

    What this tool does and does not claim is in models/README.md. Short
    version: combining photographs from separated observers gains depth,
    sub-pixel sampling, dynamic range and time-domain information. It does
    not synthesise an aperture and it does not beat the diffraction limit of
    the best contributing optic.
    """
    configure(level=log_level, json_logs=json_logs)


@main.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--inputs",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Directory of images, or a manifest.json carrying the photos rows.",
)
@click.option("--out", "out_dir", required=True, type=click.Path(path_type=Path), help="Output directory.")
@click.option("--seed", type=int, default=None, help="Override the config seed.")
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="STAGE.PARAM=VALUE",
    help="Override a stage parameter, e.g. --set coadd.epsilon=1e-3. Repeatable.",
)
@click.option("--reconstruction-id", default=None, help="Id from the reconstructions table.")
@click.option(
    "--strict-licenses/--drop-unlicensed",
    default=False,
    show_default=True,
    help="Fail on an ND-licensed input instead of dropping it.",
)
@click.option("--quiet-summary", is_flag=True, help="Do not print the JSON summary.")
def run(
    config: Path,
    inputs: Path,
    out_dir: Path,
    seed: int | None,
    overrides: tuple[str, ...],
    reconstruction_id: str | None,
    strict_licenses: bool,
    quiet_summary: bool,
) -> None:
    """Run a declarative pipeline over a set of contributions."""
    from astrostack.pipelines.runner import run_pipeline

    parsed: dict[str, dict[str, Any]] = {}
    for item in overrides:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise click.BadParameter(f"--set expects STAGE.PARAM=VALUE, got {item!r}")
        key, raw = item.split("=", 1)
        stage, param = key.split(".", 1)
        parsed.setdefault(stage, {})[param] = _coerce(raw)

    result = run_pipeline(
        config,
        inputs,
        out_dir,
        overrides=parsed or None,
        seed=seed,
        reconstruction_id=reconstruction_id,
        strict_licenses=strict_licenses,
    )

    if not quiet_summary:
        coadd = result.coadd
        _echo_json(
            {
                "pipeline": result.config.pipeline,
                "pipeline_version": resolve_git_sha(),
                "pipeline_api_version": PIPELINE_API_VERSION,
                "run_checksum": result.run_checksum,
                "out_dir": str(result.out_dir),
                "outputs": result.outputs(),
                "n_inputs": len(result.provenance.inputs),
                "n_rejected": len(result.provenance.rejected_inputs),
                "coadd": None if coadd is None else coadd.summary(),
                "metrics": result.provenance.metrics,
                "warnings": result.provenance.warnings,
            }
        )


@main.command()
@click.argument("image", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--channel", default="G", show_default=True, help="Channel/CFA plane to inspect.")
@click.option("--threshold", default=5.0, show_default=True, help="Detection threshold in sigma.")
@click.option("--no-psf", is_flag=True, help="Skip PSF characterisation (faster).")
def inspect(image: Path, channel: str, threshold: float, no_psf: bool) -> None:
    """Report metadata, astrometry and measured quality for one image."""
    from astrostack.align.stars import characterise_frame
    from astrostack.calibrate.background import estimate_background
    from astrostack.io.loaders import load_frame
    from astrostack.metrics.quality import effective_pixel_scale

    frame = load_frame(image, channel=channel)
    payload: dict[str, Any] = {
        "path": str(image),
        "format": frame.meta.source_format,
        "shape": list(frame.shape),
        "dtype": str(frame.data.dtype),
        "photometrically_unreliable": frame.meta.photometrically_unreliable,
        "unreliable_reason": frame.meta.unreliable_reason,
        "metadata": frame.meta.model_dump(exclude_none=True),
        "data_sha256": frame.checksum(),
        "statistics": {
            "min": float(np.nanmin(frame.data)),
            "median": float(np.nanmedian(frame.data)),
            "max": float(np.nanmax(frame.data)),
            "saturated_pixels": int(frame.saturated.sum()) if frame.saturated is not None else 0,
        },
    }

    bkg = estimate_background(frame.data)
    payload["background"] = bkg.describe()

    if frame.wcs is not None:
        from astrostack.align.platesolve import describe_wcs

        payload["astrometry"] = describe_wcs(frame.wcs, frame.shape)
        payload["astrometry"]["source"] = "file header"
    else:
        payload["astrometry"] = {
            "solved": False,
            "pixel_scale_prior_arcsec": frame.quality.pixel_scale_arcsec,
            "note": "not plate solved; run a pipeline with align.platesolve to attach a WCS",
        }

    if not no_psf:
        characterised = characterise_frame(frame, threshold_sigma=threshold)
        payload["quality"] = characterised.quality.model_dump(exclude_none=True)
        payload["psf_source"] = characterised.extra.get("psf_source")
        payload["psf_field_varying"] = characterised.extra.get("psf_field_varying")

    limit = frame.meta.diffraction_limit_arcsec()
    if limit:
        payload["physics"] = {
            "diffraction_limit_arcsec": limit,
            "effective_pixel_scale_arcsec": effective_pixel_scale(frame.wcs),
            "note": "no combination of images resolves finer than this (research note, section 5)",
        }
    _echo_json(payload)


@main.command()
@click.argument("a", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("b", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--channel", default="G", show_default=True)
@click.option("--threshold", default=8.0, show_default=True, help="Sigma for aperture selection.")
def metrics(a: Path, b: Path, channel: str, threshold: float) -> None:
    """Compare two images: PSNR, SSIM, flux consistency, FWHM, SNR.

    ``B`` is treated as the reference. Flux consistency is reported alongside
    PSNR/SSIM because PSNR alone is not a sufficient criterion for
    astronomical images (research note, section 4).
    """
    from astrostack.align.stars import detect_sources
    from astrostack.io.loaders import load_frame
    from astrostack.metrics.compare import compare_images
    from astrostack.metrics.quality import measure_fwhm

    fa = load_frame(a, channel=channel)
    fb = load_frame(b, channel=channel)
    if fa.shape != fb.shape:
        raise click.ClickException(
            f"shape mismatch: {fa.shape} vs {fb.shape}. Reproject onto a common grid first."
        )

    cat = detect_sources(fb.data, threshold_sigma=threshold)
    apertures = np.column_stack([cat.y, cat.x])[:200] if len(cat) else None
    cmp = compare_images(fa.data, fb.data, apertures)

    _echo_json(
        {
            "a": {"path": str(a), "quality": measure_fwhm(fa.data, fa.wcs).as_dict()},
            "b": {"path": str(b), "quality": measure_fwhm(fb.data, fb.wcs).as_dict()},
            "comparison": cmp.as_dict(),
            "n_apertures": 0 if apertures is None else len(apertures),
            "note": (
                "total_flux_ratio and source_flux_scatter matter more than PSNR for "
                "scientific use: a model can win PSNR while moving flux between sources"
            ),
        }
    )


@main.command()
def ops() -> None:
    """List the stage vocabulary available to a pipeline YAML."""
    from astrostack.pipelines.stages import OPS

    payload = {}
    for name in sorted(OPS):
        doc = (OPS[name].__doc__ or "").strip().splitlines()
        payload[name] = doc[0] if doc else ""
    _echo_json(payload)


@main.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate(config: Path) -> None:
    """Type-check a pipeline config and print its execution order."""
    from astrostack.pipelines.graph import load_pipeline, topological_order
    from astrostack.pipelines.stages import OPS

    cfg = load_pipeline(config)
    order = topological_order(cfg.active_stages())
    unknown = [s.op for s in order if s.op not in OPS]
    if unknown:
        raise click.ClickException(f"unknown ops: {sorted(set(unknown))}")
    _echo_json(
        {
            "pipeline": cfg.pipeline,
            "pipeline_api_version": cfg.pipeline_api_version,
            "seed": cfg.seed,
            "execution_order": [{"id": s.id, "op": s.op, "needs": s.needs} for s in order],
            "valid": True,
        }
    )


# The __main__ guard must stay at the very bottom, *after* every helper: with
# `python -m astrostack.cli` the module body runs top to bottom, so anything
# defined below this line would not exist yet when a command needs it.
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
