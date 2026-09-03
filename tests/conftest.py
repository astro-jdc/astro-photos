"""Fixtures compartidas de los tests transversales.

Estos tests **no** son unitarios de ningún componente: cruzan backend, models,
frontend e infra, que es justo lo que ningún agente individual podía cubrir.

Se ejecutan con el venv del backend (es el que tiene httpx, boto3 y schemathesis)
y hablan con `models/` por subproceso a través de `models/.venv`, para no mezclar
los dos entornos (regla de `CLAUDE.md`).

    backend/.venv/bin/pytest tests -q

Los tests que necesitan el stack levantado se saltan solos si no lo está, con un
mensaje que dice cómo levantarlo. Ninguno se salta en silencio.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PY = REPO_ROOT / "models" / ".venv" / "bin" / "python"
BACKEND_PY = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
FRONTEND = REPO_ROOT / "frontend"

#: Backend en marcha. `make dev`, o:
#:   cd backend && .venv/bin/uvicorn app.main:app --port 8000
API_BASE = os.environ.get("ASTRO_API_BASE", "http://127.0.0.1:8000")
API_V1 = f"{API_BASE}/api/v1"

STACK_HINT = (
    "El stack local no responde. Levántalo con:\n"
    "  podman-compose -f docker-compose.dev.yml up -d\n"
    "  cd backend && .venv/bin/alembic upgrade head\n"
    "  cd backend && .venv/bin/uvicorn app.main:app --port 8000"
)


def _backend_is_up() -> bool:
    try:
        r = httpx.get(f"{API_V1}/healthz", timeout=2.0)
    except httpx.HTTPError:
        return False
    return r.status_code == 200


@pytest.fixture(scope="session")
def api_base() -> str:
    """URL base de `/api/v1`, o skip con instrucciones si el backend no está."""
    if not _backend_is_up():
        pytest.skip(STACK_HINT, allow_module_level=True)
    return API_V1


@pytest.fixture(scope="session")
def client(api_base: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=api_base, timeout=30.0) as c:
        yield c


def run_models(code: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Ejecuta `code` con el intérprete de `models/.venv`.

    Los dos venvs no se mezclan nunca: `models/` tiene astropy y no tiene
    FastAPI; `backend/` al revés. Hablar por subproceso es lo que hace el
    worker de Batch en producción, así que además es representativo.
    """
    if not MODELS_PY.exists():  # pragma: no cover - entorno mal montado
        pytest.skip(f"No existe {MODELS_PY}; corre `make setup-models`.")
    return subprocess.run(
        [str(MODELS_PY), "-c", code],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        check=False,
    )


def run_backend(code: str) -> subprocess.CompletedProcess[str]:
    """Ejecuta `code` con el intérprete del backend, desde `backend/`."""
    return subprocess.run(
        [str(BACKEND_PY), "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "backend"),
        check=False,
    )


@pytest.fixture(scope="session")
def openapi(client: httpx.Client) -> dict[str, Any]:
    """El OpenAPI que publica el backend vivo."""
    r = client.get("/openapi.json")
    assert r.status_code == 200, r.text
    return dict(r.json())


def pytest_report_header(config: pytest.Config) -> list[str]:
    return [
        f"astro-photos tests transversales — API_BASE={API_BASE}",
        f"backend arriba: {_backend_is_up()}",
        f"python: {sys.executable}",
    ]
