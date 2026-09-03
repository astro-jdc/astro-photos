"""Provenance and attribution.

Rule 6 of the astro-ml brief and rule 4 of ``CLAUDE.md``: every output carries
``provenance.json`` (ids, checksums, weights, pipeline version, git sha,
parameters) and ``ATTRIBUTION.md`` (the authors).

The file is split into a **deterministic** part and a **volatile** part:

* ``deterministic`` — inputs, params, stage list, effective weights, output
  checksums. Given the same inputs and the same config, this block is
  byte-identical between runs. ``run_checksum`` is its sha256, and it is the
  value ``tests/test_reproducibility.py`` compares.
* ``volatile`` — timestamps, wall-clock durations, hostname, library
  versions. Useful for debugging, deliberately excluded from the checksum, so
  that running the same job twice on different days still reproduces.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrostack.version import PIPELINE_API_VERSION, __version__, resolve_git_sha

__all__ = ["ProvenanceRecorder", "StageRecord", "file_sha256", "write_attribution"]

_TRACKED_PACKAGES = (
    "numpy", "scipy", "astropy", "photutils", "reproject",
    "astroalign", "sep", "rawpy", "tifffile", "pillow",
)  # fmt: skip


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    """sha256 of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _package_versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str] = {}
    for name in _TRACKED_PACKAGES:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            try:
                out[name] = version("sep-pjw") if name == "sep" else "absent"
            except PackageNotFoundError:
                out[name] = "absent"
    return out


@dataclass(slots=True)
class StageRecord:
    """What one stage did."""

    stage_id: str
    op: str
    params: dict[str, Any]
    needs: list[str]
    duration_s: float
    summary: dict[str, Any] = field(default_factory=dict)
    flux_preserving: bool | None = None

    def deterministic(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "op": self.op,
            "needs": list(self.needs),
            "params": _jsonable(self.params),
            "summary": _jsonable(self.summary),
            "flux_preserving": self.flux_preserving,
        }


def _jsonable(value: Any) -> Any:
    """Recursively coerce to something ``json.dumps`` accepts, deterministically."""
    import numpy as np

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        if value.size <= 64:
            return [_jsonable(v) for v in value.ravel().tolist()]
        return {
            "__array__": True,
            "shape": list(value.shape),
            "dtype": str(value.dtype),
            "sha256": hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest(),
        }
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float | int | bool | str) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(exclude_none=True))
    if hasattr(value, "summary") and callable(value.summary):
        return _jsonable(value.summary())
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _jsonable(value.as_dict())
    if hasattr(value, "describe") and callable(value.describe):
        return _jsonable(value.describe())
    return str(value)


class ProvenanceRecorder:
    """Accumulates the record of a run and writes it out."""

    def __init__(
        self,
        pipeline: str,
        config_fingerprint: dict[str, Any],
        seed: int,
        reconstruction_id: str | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.config_fingerprint = config_fingerprint
        self.seed = int(seed)
        self.reconstruction_id = reconstruction_id
        self.started_at = datetime.now(UTC)
        self.stages: list[StageRecord] = []
        self.inputs: list[dict[str, Any]] = []
        self.input_locations: dict[str, str | None] = {}
        self.rejected_inputs: list[dict[str, Any]] = []
        self.outputs: dict[str, dict[str, Any]] = {}
        self.effective_weights: dict[str, float] = {}
        self.metrics: dict[str, Any] = {}
        self.warnings: list[str] = []

    # -- collection -------------------------------------------------------
    def add_input(
        self,
        photo_id: str,
        path: str | Path | None,
        license_code: str | None,
        attribution: str | None,
        data_sha256: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "photo_id": photo_id,
            "file_sha256": file_sha256(path) if path and Path(path).is_file() else None,
            "data_sha256": data_sha256,
            # The licence *at the moment of use*, per reconstruction_inputs.
            "snapshot_license": license_code,
            "attribution_name": attribution,
        }
        if extra:
            row.update(_jsonable(extra))
        self.inputs.append(row)
        # Where the bytes happened to live is provenance, but it is not part of
        # the identity of the run: the same photo re-run from a different
        # directory (or from S3 instead of a local cache) must reproduce
        # bit-for-bit. Content is identified by photo_id + file_sha256.
        self.input_locations[photo_id] = str(path) if path else None

    def add_rejected(self, photo_id: str, reason: str) -> None:
        self.rejected_inputs.append({"photo_id": photo_id, "rejection_reason": reason})

    def add_stage(self, record: StageRecord) -> None:
        self.stages.append(record)

    def add_output(self, name: str, path: str | Path, sha256: str, **extra: Any) -> None:
        self.outputs[name] = {
            "path": Path(path).name,
            "sha256": sha256,
            **_jsonable(extra),
        }

    def set_weights(self, weights: dict[str, float]) -> None:
        self.effective_weights = {k: float(v) for k, v in sorted(weights.items())}

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    # -- serialisation ----------------------------------------------------
    def deterministic_block(self) -> dict[str, Any]:
        """Everything that must be identical between two runs of the same job."""
        return {
            "pipeline": self.pipeline,
            "pipeline_api_version": PIPELINE_API_VERSION,
            "astrostack_version": __version__,
            "git_sha": resolve_git_sha(),
            "seed": self.seed,
            "config": _jsonable(self.config_fingerprint),
            "inputs": sorted(
                (_jsonable(row) for row in self.inputs), key=lambda r: str(r["photo_id"])
            ),
            "rejected_inputs": sorted(
                (_jsonable(r) for r in self.rejected_inputs), key=lambda r: str(r["photo_id"])
            ),
            "effective_weights": self.effective_weights,
            "stages": [s.deterministic() for s in self.stages],
            "outputs": _jsonable(self.outputs),
            "metrics": _jsonable(self.metrics),
            "warnings": list(self.warnings),
        }

    def run_checksum(self) -> str:
        payload = json.dumps(self.deterministic_block(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def volatile_block(self) -> dict[str, Any]:
        finished = datetime.now(UTC)
        return {
            "reconstruction_id": self.reconstruction_id,
            "input_locations": dict(sorted(self.input_locations.items())),
            "started_at": self.started_at.isoformat(),
            "finished_at": finished.isoformat(),
            "compute_seconds": (finished - self.started_at).total_seconds(),
            "stage_durations_s": {s.stage_id: round(s.duration_s, 6) for s in self.stages},
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": _package_versions(),
        }

    def to_dict(self) -> dict[str, Any]:
        det = self.deterministic_block()
        return {
            "schema": "astro-photos/provenance/1",
            "run_checksum": self.run_checksum(),
            "deterministic": det,
            "volatile": self.volatile_block(),
        }

    def write(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self.run_checksum()


def write_attribution(
    path: str | Path,
    pipeline: str,
    rows: list[dict[str, Any]],
    weights: dict[str, float] | None = None,
    output_license: str | None = None,
) -> None:
    """Write ``ATTRIBUTION.md``.

    Per rule 5 of the licence table in ``docs/data-model.md``, this file is
    generated **always** — even when every input is CC0.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    weights = weights or {}

    lines = [
        "# Attribution",
        "",
        f"This image is a derivative work produced by the `{pipeline}` pipeline of ",
        "astro-photos from the contributions listed below. Every contributor is credited ",
        "regardless of the licence they chose.",
        "",
    ]
    if output_license:
        lines += [
            f"**Licence of this derivative work:** `{output_license}` — the most restrictive ",
            "combination of the input licences.",
            "",
        ]
    lines += [
        "| photo_id | author | licence | effective weight |",
        "| --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda r: str(r.get("photo_id", ""))):
        pid = str(row.get("photo_id", ""))
        author = str(row.get("author") or row.get("attribution_name") or "unknown")
        lic = str(row.get("license") or row.get("snapshot_license") or "unspecified")
        wgt = weights.get(pid)
        lines.append(f"| `{pid}` | {author} | {lic} | {'' if wgt is None else f'{wgt:.6f}'} |")
    lines += [
        "",
        "Weights are the effective contribution of each frame to the final pixels, as ",
        "recorded in `provenance.json` and in the `reconstruction_inputs` table.",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
