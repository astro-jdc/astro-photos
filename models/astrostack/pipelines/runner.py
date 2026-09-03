"""The pipeline runner.

Executes a :class:`~astrostack.pipelines.graph.PipelineConfig` stage by stage
in a deterministic topological order, records everything into a
:class:`~astrostack.pipelines.provenance.ProvenanceRecorder`, and writes
``provenance.json`` plus ``ATTRIBUTION.md`` next to the deliverables.

Reproducibility, concretely:

* input order is ``sorted(photo_id)`` from the manifest, never the filesystem;
* stage order is the deterministic topological sort;
* every RNG stream derives from ``config.seed`` and the stage id;
* ``provenance.json`` separates the deterministic block (checksummed) from the
  volatile block (timestamps, host, library versions).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrostack.io.manifest import Manifest, load_manifest
from astrostack.logging import get_logger
from astrostack.pipelines.graph import PipelineConfig, load_pipeline, topological_order
from astrostack.pipelines.provenance import ProvenanceRecorder, StageRecord
from astrostack.pipelines.stages import RunContext, StageInputs, get_op
from astrostack.stack.base import CoaddResult

__all__ = ["PipelineRun", "run_pipeline"]

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineRun:
    """The outcome of a run."""

    config: PipelineConfig
    results: dict[str, Any]
    provenance: ProvenanceRecorder
    out_dir: Path
    run_checksum: str

    @property
    def coadd(self) -> CoaddResult | None:
        for key in sorted(self.results, reverse=True):
            if isinstance(self.results[key], CoaddResult):
                return self.results[key]
        return None

    def outputs(self) -> dict[str, Any]:
        """Output artefacts, as absolute paths.

        The stage records relative names (so the run checksum does not depend
        on where the job happened to write); this resolves them for callers.
        """
        for value in self.results.values():
            if isinstance(value, dict) and "fits" in value:
                return {
                    k: (str(self.out_dir / v) if isinstance(v, str) and not k.endswith("sha256") else v)
                    for k, v in value.items()
                }
        return {}


def _summarise(value: Any) -> dict[str, Any]:
    """A compact, JSON-safe description of a stage's result."""
    from astrostack.align.register import OutputGrid
    from astrostack.io.frame import Frame

    if isinstance(value, list) and value and isinstance(value[0], Frame):
        return {
            "n_frames": len(value),
            "frame_ids": [f.frame_id for f in value],
            "frames": [f.summary() for f in value],
        }
    if isinstance(value, CoaddResult):
        return value.summary()
    if isinstance(value, OutputGrid):
        return value.describe()
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "curve"}
    return {"value": str(value)[:200]}


def _stage_flux_flag(value: Any) -> bool | None:
    from astrostack.io.frame import Frame

    if isinstance(value, CoaddResult):
        return value.flux_preserving
    if isinstance(value, list) and value and isinstance(value[0], Frame):
        histories = [h for f in value for h in f.history]
        if not histories:
            return None
        return all("NOT-flux-preserving" not in h for h in histories)
    return None


def run_pipeline(
    config: str | Path | PipelineConfig,
    inputs: str | Path | Manifest,
    out_dir: str | Path,
    overrides: dict[str, dict[str, Any]] | None = None,
    seed: int | None = None,
    reconstruction_id: str | None = None,
    scratch: dict[str, Any] | None = None,
    strict_licenses: bool = False,
    write_provenance: bool = True,
) -> PipelineRun:
    """Run a declarative pipeline end to end.

    Parameters
    ----------
    overrides
        ``{stage_id: {param: value}}``, applied on top of the YAML. Recorded
        in the provenance fingerprint so an overridden run is never confused
        with the plain config.
    scratch
        Injected into ``ctx.scratch``; used by the training harness to feed
        in-memory frames through ``op: io.frames``.
    """
    cfg = config if isinstance(config, PipelineConfig) else load_pipeline(config)
    manifest = inputs if isinstance(inputs, Manifest) else load_manifest(inputs, strict_licenses)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    effective_seed = int(seed if seed is not None else cfg.seed)
    fingerprint = cfg.fingerprint()
    if overrides:
        fingerprint = {**fingerprint, "overrides": overrides}
    if seed is not None:
        fingerprint = {**fingerprint, "seed": effective_seed}

    recorder = ProvenanceRecorder(
        pipeline=cfg.pipeline,
        config_fingerprint=fingerprint,
        seed=effective_seed,
        reconstruction_id=reconstruction_id,
    )
    ctx = RunContext(
        seed=effective_seed,
        out_dir=out,
        manifest=manifest,
        provenance=recorder,
        pipeline=cfg.pipeline,
        scratch=dict(scratch or {}),
    )

    order = topological_order(cfg.active_stages())
    log.info(
        "pipeline_start",
        pipeline=cfg.pipeline,
        stages=[s.id for s in order],
        n_inputs=len(manifest),
        seed=effective_seed,
    )

    results: dict[str, Any] = {}
    for spec in order:
        params = cfg.effective_params(spec)
        params.update((overrides or {}).get(spec.id, {}))
        fn = get_op(spec.op)
        # Only pass params the op actually accepts, so shared `defaults` can
        # carry keys that are meaningful to some stages and not to others.
        import inspect

        signature = inspect.signature(fn)
        accepted = {
            k: v
            for k, v in params.items()
            if k in signature.parameters
            or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
            )
        }
        stage_inputs = StageInputs({dep: results[dep] for dep in spec.needs})
        ctx.stage_id = spec.id

        started = time.perf_counter()
        log.info("stage_start", stage=spec.id, op=spec.op)
        value = fn(ctx, stage_inputs, **accepted)
        duration = time.perf_counter() - started
        results[spec.id] = value

        recorder.add_stage(
            StageRecord(
                stage_id=spec.id,
                op=spec.op,
                params=accepted,
                needs=list(spec.needs),
                duration_s=duration,
                summary=_summarise(value),
                flux_preserving=_stage_flux_flag(value),
            )
        )
        log.info("stage_done", stage=spec.id, seconds=round(duration, 3))

    checksum = recorder.run_checksum()
    if write_provenance:
        recorder.write(out / "provenance.json")
    log.info("pipeline_done", pipeline=cfg.pipeline, run_checksum=checksum)

    return PipelineRun(
        config=cfg, results=results, provenance=recorder, out_dir=out, run_checksum=checksum
    )
