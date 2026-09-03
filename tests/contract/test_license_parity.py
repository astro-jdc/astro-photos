"""La lógica de licencias duplicada en el frontend no puede divergir.

`CLAUDE.md`, regla dura 5: la combinación de licencias vive en un único sitio,
`backend/app/domain/licensing.py`. El frontend tiene una réplica
(`app/lib/licensing.ts::resolveOutputLicenseHint`) para pintar la interfaz sin
esperar al servidor; está documentada como pista optimista y no decide nada,
pero es una segunda implementación de una regla con consecuencias legales.

Este fichero convierte esa duplicación en algo vigilado:

1. genera la tabla de verdad desde la función de dominio del backend,
2. la compara con el fixture que consume el test de vitest,
3. ejecuta ese test de vitest, que recorre la tabla entera.

Si alguien toca cualquiera de las dos implementaciones, esto falla.

    backend/.venv/bin/pytest tests/contract/test_license_parity.py -q
"""

from __future__ import annotations

import itertools
import json
import subprocess

import pytest

from app.domain.licensing import (
    LICENSE_CATALOG,
    PhotoLicenseFacts,
    resolve_output_license,
)
from tests.conftest import FRONTEND

pytestmark = pytest.mark.contract

FIXTURE = FRONTEND / "tests" / "fixtures" / "license-table.json"
SPEC = "tests/unit/licenseParity.spec.ts"

#: Combinaciones de hasta 3 entradas sobre las 8 licencias del catálogo.
#: Con 3 basta: las reglas de combinación son idempotentes y conmutativas
#: (NC y SA son contagiosos), así que un cuarto elemento no añade casos nuevos.
MAX_INPUTS = 3


def _expected_table() -> dict:
    codes = [info.code for info in LICENSE_CATALOG]
    cases = []
    for n in range(1, MAX_INPUTS + 1):
        for combo in itertools.combinations_with_replacement(codes, n):
            resolution = resolve_output_license(
                [PhotoLicenseFacts(photo_id=f"p{i}", license=c) for i, c in enumerate(combo)]
            )
            cases.append(
                {
                    "inputs": [c.value for c in combo],
                    "resulting_license": (
                        resolution.resulting_license.value
                        if resolution.resulting_license
                        else None
                    ),
                    "blocked": [b.photo_id for b in resolution.blocked],
                }
            )
    return {
        "_comment": (
            "GENERADO por tests/contract/test_license_parity.py desde "
            "backend/app/domain/licensing.py. No editar a mano."
        ),
        "cases": cases,
    }


def test_el_fixture_del_frontend_esta_al_dia() -> None:
    """El fixture es exactamente lo que dice hoy la función de dominio.

    Si falla, se regenera corriendo este mismo test con `--regenerate-license-table`
    o copiando la tabla que imprime el mensaje de error.
    """
    expected = _expected_table()
    assert FIXTURE.exists(), (
        f"Falta {FIXTURE}. Genéralo desde `backend/app/domain/licensing.py`."
    )
    actual = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert actual["cases"] == expected["cases"], (
        "La tabla de licencias del frontend no coincide con la función de dominio "
        "del backend. Regenera el fixture:\n"
        f"    {FIXTURE}\n"
        "y vuelve a pasar `pnpm test`."
    )


def test_la_tabla_cubre_las_ocho_licencias() -> None:
    """Un fixture que solo cubriera dos licencias no probaría gran cosa."""
    cases = _expected_table()["cases"]
    covered = {code for case in cases for code in case["inputs"]}
    assert len(covered) == 8, f"La tabla solo cubre {len(covered)} licencias: {covered}"
    assert len(cases) >= 100, f"Solo {len(cases)} combinaciones; se esperaban más de 100."


def test_hay_casos_de_bloqueo_y_casos_de_licencia_resultante() -> None:
    """Control: la tabla tiene que contener los dos desenlaces.

    Si todas las combinaciones bloqueasen (o ninguna), el test de vitest pasaría
    por una rama sola y no comprobaría la otra.
    """
    cases = _expected_table()["cases"]
    blocked = [c for c in cases if c["blocked"]]
    resolved = [c for c in cases if not c["blocked"]]
    assert blocked, "Ninguna combinación bloquea: falta cubrir ND/ARR."
    assert resolved, "Ninguna combinación resuelve licencia."
    # ND y ARR bloquean, así que cualquier combinación que los incluya está en `blocked`.
    for case in resolved:
        assert not ({"CC-BY-ND-4.0", "CC-BY-NC-ND-4.0", "ARR"} & set(case["inputs"])), (
            f"Una combinación con ND/ARR no está bloqueada: {case}"
        )


@pytest.mark.slow
def test_el_frontend_pasa_la_tabla_entera() -> None:
    """Ejecuta el test de vitest que recorre el fixture. La prueba de verdad."""
    proc = subprocess.run(
        ["pnpm", "exec", "vitest", "run", SPEC],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "La pista de licencias del frontend diverge del backend:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
