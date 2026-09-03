"""Fixtures que hablan con Postgres y MinIO de verdad."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

from tests.conftest import run_backend

__all__ = ["ApiUser", "make_user"]


class ApiUser:
    """Un usuario recién creado en la base de datos, con su JWT."""

    def __init__(self, user_id: str, sub: str, email: str, token: str) -> None:
        self.id = user_id
        self.sub = sub
        self.email = email
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


_CREATE_USER = """
import asyncio, json, sys, uuid
from sqlalchemy import text
from app.core.config import get_settings
from app.core.security import create_local_token
from app.db.session import get_engine

sub, email, display = sys.argv[1], sys.argv[2], sys.argv[3]

async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "INSERT INTO users (email, cognito_sub, display_name, attribution_name) "
                "VALUES (:email, :sub, :display, :attribution) RETURNING id"
            ),
            {"email": email, "sub": sub, "display": display, "attribution": display},
        )
        user_id = str(row.scalar_one())
    await engine.dispose()
    token = create_local_token(sub=sub, user_id=user_id, email=email)
    print(json.dumps({"id": user_id, "sub": sub, "email": email, "token": token}))

asyncio.run(main())
"""


def make_user(prefix: str = "qa") -> ApiUser:
    """Crea un usuario real y devuelve su token.

    Se hace por subproceso con el intérprete del backend para reutilizar su
    configuración (`DATABASE_URL`, `JWT_SECRET`) sin duplicarla aquí.
    """
    import json
    import subprocess

    from tests.conftest import BACKEND_PY, REPO_ROOT

    marker = uuid.uuid4().hex[:12]
    sub = f"{prefix}-{marker}"
    email = f"{prefix}-{marker}@qa.astro-photos.test"
    proc = subprocess.run(
        [str(BACKEND_PY), "-c", _CREATE_USER, sub, email, f"QA {marker}"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "backend"),
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"No se pudo crear el usuario de prueba:\n{proc.stdout}\n{proc.stderr}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return ApiUser(payload["id"], payload["sub"], payload["email"], payload["token"])


@pytest.fixture
def user() -> ApiUser:
    return make_user()


@pytest.fixture
def other_user() -> ApiUser:
    return make_user("qa-other")


@pytest.fixture
def auth_client(api_base: str, user: ApiUser) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=api_base, headers=user.headers, timeout=60.0) as c:
        yield c


# Reexportado para que `run_backend` esté disponible en los tests del paquete.
__all__ += ["run_backend"]
