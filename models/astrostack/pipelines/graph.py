"""The declarative pipeline graph.

A pipeline is a YAML file listing stages, their parameters and their
dependencies. Nothing about the *order of execution* is left to chance: the
topological sort breaks ties by declaration index, so two runs of the same
file always execute the same stages in the same order. That matters more than
it looks: floating-point summation is not associative, and a different visit
order would produce a different last bit and break the reproducibility
contract.

Schema::

    pipeline: classical-stack-v1        # matches reconstructions.pipeline
    pipeline_api_version: "1"
    seed: 20240101                      # root of every derived RNG stream
    description: free text
    defaults: {channel: G}              # merged into every stage's params
    outputs:
      fits: coadd.fits
      preview: preview.png
    stages:
      - id: load
        op: io.load
        params: {...}
      - id: coadd
        op: stack.optimal
        needs: [load]
        params: {...}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from astrostack.errors import PipelineConfigError

__all__ = ["PipelineConfig", "StageSpec", "load_pipeline", "topological_order"]


class StageSpec(BaseModel):
    """One node of the graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    op: str
    needs: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    description: str | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"stage id {v!r} must be alphanumeric (dashes and underscores ok)")
        return v


class PipelineConfig(BaseModel):
    """A whole pipeline file."""

    model_config = ConfigDict(extra="forbid")

    pipeline: str
    pipeline_api_version: str = "1"
    seed: int = 0
    description: str | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    stages: list[StageSpec]

    @field_validator("stages")
    @classmethod
    def _non_empty(cls, v: list[StageSpec]) -> list[StageSpec]:
        if not v:
            raise ValueError("a pipeline needs at least one stage")
        ids = [s.id for s in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate stage ids: {sorted(dupes)}")
        known = set(ids)
        for s in v:
            missing = [n for n in s.needs if n not in known]
            if missing:
                raise ValueError(f"stage {s.id!r} depends on unknown stages {missing}")
        return v

    def active_stages(self) -> list[StageSpec]:
        """Stages with ``enabled: true``, with disabled deps pruned."""
        enabled = {s.id for s in self.stages if s.enabled}
        out = []
        for s in self.stages:
            if not s.enabled:
                continue
            spec = s.model_copy(update={"needs": [n for n in s.needs if n in enabled]})
            out.append(spec)
        return out

    def effective_params(self, stage: StageSpec) -> dict[str, Any]:
        """Stage params with ``defaults`` merged underneath."""
        merged = dict(self.defaults)
        merged.update(stage.params)
        return merged

    def fingerprint(self) -> dict[str, Any]:
        """The part of the config that affects the output bytes.

        ``description`` and per-stage ``description`` are excluded, so editing
        a comment does not invalidate a cached reconstruction.
        """
        return {
            "pipeline": self.pipeline,
            "pipeline_api_version": self.pipeline_api_version,
            "seed": self.seed,
            "defaults": self.defaults,
            "stages": [
                {"id": s.id, "op": s.op, "needs": list(s.needs), "params": s.params}
                for s in self.active_stages()
            ],
        }


def load_pipeline(path: str | Path) -> PipelineConfig:
    """Parse and validate a pipeline YAML."""
    p = Path(path)
    if not p.exists():
        raise PipelineConfigError(f"pipeline config not found: {p}")
    try:
        payload = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PipelineConfigError(f"{p}: invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise PipelineConfigError(f"{p}: top level must be a mapping")
    try:
        return PipelineConfig(**payload)
    except Exception as exc:
        raise PipelineConfigError(f"{p}: {exc}") from exc


def topological_order(stages: list[StageSpec]) -> list[StageSpec]:
    """Deterministic topological sort (Kahn, ties broken by declaration order)."""
    index = {s.id: i for i, s in enumerate(stages)}
    by_id = {s.id: s for s in stages}
    remaining = {s.id: set(s.needs) for s in stages}
    ready = sorted([sid for sid, deps in remaining.items() if not deps], key=lambda s: index[s])

    order: list[StageSpec] = []
    while ready:
        sid = ready.pop(0)
        order.append(by_id[sid])
        del remaining[sid]
        newly = []
        for other, deps in remaining.items():
            if sid in deps:
                deps.discard(sid)
                if not deps:
                    newly.append(other)
        ready = sorted(ready + newly, key=lambda s: index[s])

    if remaining:
        raise PipelineConfigError(
            f"pipeline graph has a cycle involving stages {sorted(remaining)}"
        )
    return order
