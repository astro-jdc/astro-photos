"""Input manifests.

A reconstruction's inputs arrive either as a directory of files or as a
``manifest.json`` produced by the backend, which carries the ``photos`` rows
(licence, optics, GPS, timestamps) that a bare file on disk does not have.

Two invariants live here:

* **Deterministic order.** Frames are sorted by ``photo_id``, never by the
  order ``Path.iterdir`` happens to yield. Coaddition is float arithmetic and
  is therefore *not* associative; input order changes the last bits of the
  output, which would break the reproducibility contract.
* **Licence gating.** ND-licensed photos and photos with
  ``allow_derivatives_in_stacks = false`` are refused outright rather than
  degrading the output licence (rule 3/4 of the licence table in
  ``docs/data-model.md``). The authoritative combination logic stays in the
  backend; this is a local safety net for offline runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrostack.errors import LicenseViolation, PipelineConfigError
from astrostack.io.frame import FrameMetadata
from astrostack.io.loaders import (
    FITS_EXTENSIONS,
    LOSSY_EXTENSIONS,
    RAW_EXTENSIONS,
    TIFF_EXTENSIONS,
)
from astrostack.logging import get_logger

__all__ = ["InputSpec", "Manifest", "load_manifest"]

log = get_logger(__name__)

SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | FITS_EXTENSIONS | TIFF_EXTENSIONS | LOSSY_EXTENSIONS

#: Licence codes that forbid derivative works. Such a photo cannot enter a
#: stack at all: a coadd *is* a derivative.
NO_DERIVATIVES_CODES = frozenset({"CC-BY-ND-4.0", "CC-BY-NC-ND-4.0", "ND", "ARR", "all-rights-reserved"})


@dataclass(slots=True)
class InputSpec:
    """One candidate input: a path plus the metadata the backend knows."""

    path: Path
    meta: FrameMetadata

    @property
    def photo_id(self) -> str:
        return self.meta.photo_id


@dataclass(slots=True)
class Manifest:
    """An ordered, licence-checked set of inputs."""

    inputs: list[InputSpec]
    rejected: list[dict[str, str]] = field(default_factory=list)
    source: str = ""
    #: Licencia de la obra derivada, **calculada por el backend** con
    #: ``resolve_output_license()`` y transportada en el manifiesto. ``models``
    #: la escribe en ``ATTRIBUTION.md`` y en las cabeceras del FITS, pero no la
    #: decide: esa lógica vive en un único sitio (regla dura 5 de ``CLAUDE.md``).
    output_license: str | None = None

    def __len__(self) -> int:
        return len(self.inputs)

    def photo_ids(self) -> list[str]:
        return [spec.photo_id for spec in self.inputs]

    def attribution_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "photo_id": s.meta.photo_id,
                "author": s.meta.attribution_name or s.meta.owner_display_name or "unknown",
                "license": s.meta.license,
                "source_path": str(s.path),
            }
            for s in self.inputs
        ]


def _license_ok(meta: FrameMetadata) -> str | None:
    """Return a rejection reason, or ``None`` if the photo may be used."""
    if not meta.allow_derivatives_in_stacks:
        return "owner disabled allow_derivatives_in_stacks"
    code = (meta.license or "").strip()
    if code and (code in NO_DERIVATIVES_CODES or "-ND-" in code.upper() or code.upper().endswith("-ND")):
        return f"licence {code} forbids derivative works; a coadd is a derivative"
    return None


def _scan_directory(root: Path) -> list[InputSpec]:
    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.startswith(".")
    ]
    # Sort by the stable id we will use downstream, not by filesystem order.
    return [InputSpec(path=p, meta=FrameMetadata(photo_id=p.stem)) for p in sorted(files, key=lambda p: (p.stem, str(p)))]


def _read_manifest_file(path: Path) -> tuple[list[InputSpec], str | None]:
    """Devuelve ``(specs, output_license)``.

    El manifiesto puede ser una lista de entradas o un objeto con ``inputs``.
    Solo la segunda forma puede transportar ``output_license``, que es la que
    escribe el backend.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    output_license: str | None = None
    if isinstance(payload, dict):
        raw_license = payload.get("output_license")
        output_license = str(raw_license) if raw_license else None
        payload = payload.get("inputs") or payload.get("photos") or []
    if not isinstance(payload, list):
        raise PipelineConfigError(f"{path}: manifest must be a list, or an object with 'inputs'")

    base = path.parent
    specs: list[InputSpec] = []
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise PipelineConfigError(f"{path}: entry {i} is not an object")
        raw_path = entry.get("path") or entry.get("s3_key_original") or entry.get("file")
        if not raw_path:
            raise PipelineConfigError(f"{path}: entry {i} has no 'path'")
        p = Path(raw_path)
        if not p.is_absolute():
            p = (base / p).resolve()
        fields = {k: v for k, v in entry.items() if k not in {"path", "file", "s3_key_original"}}
        fields.setdefault("photo_id", p.stem)
        specs.append(InputSpec(path=p, meta=FrameMetadata(**fields)))
    return specs, output_license


def load_manifest(source: str | Path, strict_licenses: bool = True) -> Manifest:
    """Build a :class:`Manifest` from a directory or a ``manifest.json``.

    Parameters
    ----------
    strict_licenses
        When ``True`` (the default) an ND photo raises
        :class:`~astrostack.errors.LicenseViolation`. When ``False`` it is
        recorded in ``Manifest.rejected`` and dropped, which is what a batch
        job wants: one bad licence should not fail 400 good frames.
    """
    src = Path(source)
    output_license: str | None = None
    if src.is_dir():
        specs = _scan_directory(src)
    elif src.is_file() and src.suffix.lower() == ".json":
        specs, output_license = _read_manifest_file(src)
    elif src.is_file():
        specs = [InputSpec(path=src, meta=FrameMetadata(photo_id=src.stem))]
    else:
        raise PipelineConfigError(f"input source does not exist: {src}")

    if not specs:
        raise PipelineConfigError(f"no supported image files found under {src}")

    kept: list[InputSpec] = []
    rejected: list[dict[str, str]] = []
    for spec in specs:
        reason = _license_ok(spec.meta)
        if reason is None:
            kept.append(spec)
            continue
        if strict_licenses:
            raise LicenseViolation(f"{spec.photo_id}: {reason}")
        rejected.append({"photo_id": spec.photo_id, "reason": reason})
        log.warning("input_rejected", photo_id=spec.photo_id, reason=reason)

    seen: set[str] = set()
    for spec in kept:
        if spec.photo_id in seen:
            raise PipelineConfigError(f"duplicate photo_id in manifest: {spec.photo_id!r}")
        seen.add(spec.photo_id)

    kept.sort(key=lambda s: s.photo_id)
    if not kept:
        raise PipelineConfigError(f"every input under {src} was rejected: {rejected}")
    return Manifest(
        inputs=kept, rejected=rejected, source=str(src), output_license=output_license
    )
