"""`cdk synth` tiene que funcionar para los dos entornos.

Hay dos niveles:

* `test_synth_de_<entorno>` sintetiza el árbol en proceso (rápido, sin node).
* `test_cdk_synth_cli_*` invoca el CLI de verdad, que es lo que corre el CI. Se
  salta solo si no hay CLI instalado, para no romper un `pytest` en local.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest

from tests.conftest import ENVIRONMENTS, build_app

INFRA_DIR = Path(__file__).resolve().parent.parent

EXPECTED_STACKS = {
    "Network",
    "Ecr",
    "Data",
    "Auth",
    "Compute",
    "Api",
    "EdgeGlobal",
    "Edge",
    "Observability",
}


@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_synth_del_arbol_completo(env_name: str, tmp_path: Path) -> None:
    app, _stacks = build_app(env_name)
    assembly = app.synth(force=True)

    names = {artifact.stack_name for artifact in assembly.stacks}
    assert names == {f"AstroPhotos-{env_name}-{c}" for c in EXPECTED_STACKS}

    for artifact in assembly.stacks:
        template = json.loads(Path(artifact.template_full_path).read_text())
        assert template["Resources"], f"{artifact.stack_name} no tiene recursos"


@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_synth_no_necesita_credenciales(env_name: str) -> None:
    """Ni un solo *context lookup*.

    Si el árbol pidiera zonas de disponibilidad, una zona de Route 53 o una AMI,
    la asamblea traería entradas en `missing` y `cdk synth` fallaría en el CI, que
    no tiene credenciales. `stacks/base.py` existe justo para evitarlo.
    """
    app, _ = build_app(env_name)
    assembly = app.synth(force=True)
    manifest = json.loads((Path(assembly.directory) / "manifest.json").read_text())
    assert not manifest.get("missing"), manifest.get("missing")


def test_falta_el_contexto_env() -> None:
    app = cdk.App()
    with pytest.raises(ValueError, match="env=staging"):
        import config

        config.load(app)


def test_entorno_desconocido() -> None:
    app = cdk.App(context={"env": "produccion"})
    with pytest.raises(ValueError, match="desconocido"):
        import config

        config.load(app)


def _cdk_cli() -> list[str] | None:
    """Devuelve cómo invocar el CLI de CDK, o `None` si no está disponible."""
    # `aws-cdk-cli` (requirements.txt) instala el CLI dentro del venv.
    venv_cli = Path(sys.executable).parent / "cdk"
    if venv_cli.exists():
        return [str(venv_cli)]
    found = shutil.which("cdk")
    if found:
        return [found]
    return None


@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_cdk_synth_cli(env_name: str, tmp_path: Path) -> None:
    """El comando exacto que corre el CI: `cdk synth -c env=...`."""
    cli = _cdk_cli()
    if cli is None:
        pytest.skip("CLI de CDK no instalado: pip install -r infra/requirements.txt")

    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
    env.setdefault("CDK_DEFAULT_ACCOUNT", "111122223333")
    env.setdefault("CDK_DEFAULT_REGION", "eu-west-1")

    result = subprocess.run(
        [*cli, "synth", "-c", f"env={env_name}", "--quiet", "--output", str(tmp_path / "out")],
        cwd=INFRA_DIR,
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    produced = {p.name for p in (tmp_path / "out").glob("*.template.json")}
    assert len(produced) == len(EXPECTED_STACKS), produced
