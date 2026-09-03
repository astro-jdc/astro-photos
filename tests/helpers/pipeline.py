"""Ejecutar pipelines reales de `astrostack` desde los tests transversales.

Los dos venvs no se mezclan (regla de `CLAUDE.md`), así que `models/` se invoca
por subproceso con su propio intérprete — que es exactamente lo que hace el job
de AWS Batch en producción.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import MODELS_PY, REPO_ROOT

__all__ = ["PipelineFailed", "PipelineRun", "build_corpus", "run_astrostack"]

MODELS_DIR = REPO_ROOT / "models"
CONFIGS = MODELS_DIR / "configs"


class PipelineFailed(AssertionError):
    """El subproceso de `models/` terminó con error. Lleva el stderr entero."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"El subproceso de models falló (rc={returncode}):\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )


def _run(code: str, *args: str) -> str:
    """Ejecuta `code` bajo `models/.venv` con cwd en `models/`.

    `cwd=models/` es lo que permite `from tests.synthetic import ...`, que es el
    generador de corpus sintéticos con verdad conocida que ya usa `models/`.

    Un fallo levanta :class:`PipelineFailed` en vez de `pytest.fail` para que un
    test pueda *esperar* el fallo (el caso del ND, que debe reventar).
    """
    if not MODELS_PY.exists():  # pragma: no cover
        pytest.skip(f"No existe {MODELS_PY}; corre `make setup-models`.")
    proc = subprocess.run(
        [str(MODELS_PY), "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=str(MODELS_DIR),
        check=False,
    )
    if proc.returncode != 0:
        raise PipelineFailed(proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout


_BUILD_CORPUS = """
import json, sys
sys.path.insert(0, ".")
from tests.synthetic import make_corpus, write_corpus

out_dir, n_frames, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
licenses = json.loads(sys.argv[4])
output_license = sys.argv[5] or None

field = make_corpus(
    n_frames=n_frames, shape=(96, 96), n_stars=12, seed=seed,
    fwhm_pixels=3.0, sky_level=220.0, dither_pixels=1.2,
)
directory, manifest_path = write_corpus(field, out_dir)

# El manifiesto que produce el backend lleva la licencia y la autoría de cada
# foto. `write_corpus` pone la misma para todas; aquí se sobreescribe una a una
# para poder montar mezclas de licencias.
entries = json.loads(open(manifest_path).read())
for i, entry in enumerate(entries):
    if i < len(licenses):
        entry["license"] = licenses[i]
    entry["attribution_name"] = f"Autora {i}"

# Forma "objeto": es la que emite el backend, y la única que puede transportar
# `output_license` — la licencia que resolvió `resolve_output_license()`.
document = {"output_license": output_license, "inputs": entries}
open(manifest_path, "w").write(json.dumps(document, indent=2))

print(json.dumps({
    "directory": str(directory),
    "manifest": str(manifest_path),
    "photo_ids": [e["photo_id"] for e in entries],
    "licenses": [e["license"] for e in entries],
    "authors": [e["attribution_name"] for e in entries],
    "output_license": output_license,
}))
"""


def build_corpus(
    out_dir: Path,
    *,
    n_frames: int = 4,
    seed: int = 20260903,
    licenses: list[str] | None = None,
    resolve_license: bool = True,
) -> dict[str, Any]:
    """Corpus sintético en disco (FITS + `manifest.json`), con licencias a medida.

    Con `resolve_license` (por defecto) la licencia de salida se calcula con la
    **función de dominio del backend**, igual que haría el servicio real antes
    de encolar el job, y se escribe en el manifiesto. Así el test recorre el
    camino entero: backend decide -> manifiesto transporta -> models escribe.
    """
    output_license = ""
    if resolve_license and licenses:
        from app.domain.licensing import (
            LicenseCode,
            PhotoLicenseFacts,
            resolve_output_license,
        )

        resolution = resolve_output_license(
            [
                PhotoLicenseFacts(photo_id=f"p{i}", license=LicenseCode(code))
                for i, code in enumerate(licenses)
            ]
        )
        if resolution.resulting_license is not None:
            output_license = resolution.resulting_license.value

    raw = _run(
        _BUILD_CORPUS,
        str(out_dir),
        str(n_frames),
        str(seed),
        json.dumps(licenses or []),
        output_license,
    )
    return dict(json.loads(raw.strip().splitlines()[-1]))


_RUN_PIPELINE = """
import json, sys
sys.path.insert(0, ".")
from astrostack.pipelines.runner import run_pipeline

config, manifest, out_dir, strict = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
result = run_pipeline(config, manifest, out_dir, strict_licenses=strict)
print(json.dumps({
    "run_checksum": result.run_checksum,
    "out_dir": str(result.out_dir),
    "outputs": result.outputs(),
    "n_inputs": len(result.provenance.inputs),
    "n_rejected": len(result.provenance.rejected_inputs),
    "rejected": [r.get("photo_id") for r in result.provenance.rejected_inputs],
}))
"""


class PipelineRun:
    """Lo que dejó una ejecución en disco."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.run_checksum: str = payload["run_checksum"]
        self.out_dir = Path(payload["out_dir"])
        self.outputs: dict[str, Any] = payload["outputs"]
        self.n_inputs: int = payload["n_inputs"]
        self.n_rejected: int = payload["n_rejected"]
        self.rejected: list[str] = payload["rejected"]

    @property
    def attribution(self) -> str:
        return (self.out_dir / "ATTRIBUTION.md").read_text(encoding="utf-8")

    @property
    def fits_path(self) -> Path:
        return self.out_dir / "coadd.fits"

    def fits_bytes(self) -> bytes:
        return self.fits_path.read_bytes()

    def provenance(self) -> dict[str, Any]:
        return dict(json.loads((self.out_dir / "provenance.json").read_text(encoding="utf-8")))


def run_astrostack(
    manifest: Path | str,
    out_dir: Path | str,
    *,
    config: str = "classical-stack-v1",
    strict_licenses: bool = True,
) -> PipelineRun:
    """Corre un pipeline declarativo de `models/` de verdad."""
    cfg = CONFIGS / f"{config}.yaml"
    raw = _run(
        _RUN_PIPELINE,
        str(cfg),
        str(manifest),
        str(out_dir),
        "1" if strict_licenses else "0",
    )
    return PipelineRun(json.loads(raw.strip().splitlines()[-1]))


_FITS_HEADER = """
import json, sys
sys.path.insert(0, ".")
from astropy.io import fits

path = sys.argv[1]
out = {}
with fits.open(path) as hdul:
    for hdu in hdul:
        name = hdu.header.get("EXTNAME", "PRIMARY")
        out[name] = {
            "cards": {k: str(v) for k, v in hdu.header.items() if k != "HISTORY"},
            "history": [str(h) for h in hdu.header.get("HISTORY", [])],
        }
print(json.dumps(out))
"""


def fits_headers(path: Path | str) -> dict[str, Any]:
    """Cabeceras de todas las extensiones de un FITS, leídas con astropy real."""
    return dict(json.loads(_run(_FITS_HEADER, str(path)).strip().splitlines()[-1]))
