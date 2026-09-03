"""Fixtures compartidas. Nada aquí toca la red ni AWS."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-one")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://astro:astro@localhost:5432/astrophotos_test"
)


@pytest.fixture(scope="session")
def settings() -> Any:
    from app.core.config import Settings

    return Settings()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def fake_user(user_id: uuid.UUID) -> Any:
    """Un ``User`` en memoria: no se persiste, solo se le leen atributos."""
    from app.core.security import Role
    from app.domain.licensing import LicenseCode
    from app.models.user import User

    user = User(
        id=user_id,
        email="observador@example.org",
        display_name="Observador de Prueba",
        default_license=LicenseCode.CC_BY_NC,
        role=Role.MEMBER,
        storage_quota_bytes=21_474_836_480,
        storage_used_bytes=0,
        is_active=True,
        attribution_name="Observador de Prueba",
    )
    user.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    user.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return user


@pytest.fixture
def auth_headers(user_id: uuid.UUID, settings: Any) -> dict[str, str]:
    from app.core.security import create_local_token

    token = create_local_token(
        sub="test-sub",
        user_id=user_id,
        email="observador@example.org",
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def integration_database_url() -> str | None:
    """URL de una base PostGIS real para los tests de integración, si la hay."""
    return os.environ.get("DATABASE_URL_TEST")


def _testcontainers_importable() -> bool:
    """El módulo cambió de sitio entre versiones; se aceptan las dos ubicaciones."""
    import importlib

    for module in ("testcontainers.community.postgres", "testcontainers.postgres"):
        try:
            importlib.import_module(module)
        except ImportError:
            continue
        return True
    return False


def _containers_available() -> bool:
    """¿Hay realmente un runtime de contenedores utilizable?

    No basta con que el binario ``docker``/``podman`` exista: testcontainers habla
    con el **socket** de la API de Docker, y en una máquina con podman rootless sin
    el servicio activo el binario está pero el socket no. Por eso se comprueba
    creando el mismo cliente que usará testcontainers.
    """
    if os.environ.get("DATABASE_URL_TEST"):
        return True
    if not _testcontainers_importable():
        return False
    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
    except Exception:
        return False
    return True


CONTAINERS_AVAILABLE = _containers_available()

#: Marca compartida por todos los tests que necesitan una base PostGIS real.
requires_postgis = pytest.mark.skipif(
    not CONTAINERS_AVAILABLE,
    reason=(
        "No hay contenedores disponibles ni DATABASE_URL_TEST. Levanta el stack con "
        "`make up` y exporta DATABASE_URL_TEST, o instala docker/podman."
    ),
)


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Los servicios de infraestructura son singletons: se limpian entre tests."""
    from app.api.deps import reset_infrastructure
    from app.core.config import get_settings

    reset_infrastructure()
    yield
    reset_infrastructure()
    get_settings.cache_clear()
