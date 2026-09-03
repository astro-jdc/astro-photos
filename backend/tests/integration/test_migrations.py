"""``alembic upgrade head`` contra un PostGIS real, y coherencia con los modelos.

Igual que el resto de integración: se salta si no hay contenedores ni
``DATABASE_URL_TEST``.

Alembic se invoca por **subproceso** y no en el mismo intérprete a propósito:
``migrations/env.py`` abre su propio bucle con ``asyncio.run()``, y eso no se puede
anidar dentro del bucle que ya tiene pytest-asyncio. De paso, el test ejerce el
mismo comando que corre ``make migrate``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.conftest import requires_postgis

pytestmark = [pytest.mark.integration, requires_postgis]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC = BACKEND_ROOT / ".venv" / "bin" / "alembic"


def _postgres_container_class() -> type:
    """El módulo de testcontainers cambió de sitio; se aceptan las dos ubicaciones."""
    import importlib

    for module in ("testcontainers.community.postgres", "testcontainers.postgres"):
        try:
            return importlib.import_module(module).PostgresContainer  # type: ignore[no-any-return]
        except ImportError:
            continue
    pytest.skip("testcontainers no está disponible")


def _run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [str(ALEMBIC), *args] if ALEMBIC.exists() else [sys.executable, "-m", "alembic", *args]
    # Binario del propio venv y argv explícito: sin shell y sin entrada del usuario.
    return subprocess.run(
        argv,
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    """URL de una base efímera; el contenedor vive durante todo el módulo."""
    url = os.environ.get("DATABASE_URL_TEST")
    if url is not None:
        yield url
        return
    container_cls = _postgres_container_class()
    container = container_cls("postgis/postgis:16-3.4", driver="asyncpg")
    container.start()
    try:
        yield str(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture(scope="module")
def migrated_url(database_url: str) -> Iterator[str]:
    """Aplica ``alembic upgrade head`` sobre una base limpia."""
    _run_alembic(database_url, "downgrade", "base")
    result = _run_alembic(database_url, "upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade head falló:\n{result.stderr}"
    yield database_url


@pytest_asyncio.fixture
async def conn(migrated_url: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(migrated_url)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def test_upgrade_head_creates_every_table(conn: AsyncConnection) -> None:
    tables = set(
        (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")))
        .scalars()
        .all()
    )
    assert {
        "users",
        "photos",
        "sky_objects",
        "observing_sites",
        "collections",
        "collection_photos",
        "reconstructions",
        "reconstruction_inputs",
        "models",
        "training_runs",
        "dataset_snapshots",
        "licenses",
        "audit_log",
        "alembic_version",
    } <= tables


async def test_all_documented_enums_exist(conn: AsyncConnection) -> None:
    types = set(
        (
            await conn.execute(
                text(
                    "SELECT typname FROM pg_type t JOIN pg_namespace n "
                    "ON n.oid = t.typnamespace WHERE t.typtype = 'e' "
                    "AND n.nspname = 'public'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {
        "license_code",
        "user_role",
        "photo_status",
        "time_source",
        "location_source",
        "location_precision",
        "job_status",
        "object_catalog",
        "object_type",
        "model_architecture",
    } <= types


async def test_upgrade_head_seeds_the_license_catalog(conn: AsyncConnection) -> None:
    """La tabla ``licenses`` debe coincidir exactamente con el catálogo de dominio."""
    from app.domain.licensing import LICENSE_CATALOG

    rows = (
        await conn.execute(
            text(
                "SELECT code, allows_commercial, allows_derivatives, "
                "requires_sharealike, restrictiveness FROM licenses"
            )
        )
    ).all()
    seeded = {row[0]: tuple(row[1:]) for row in rows}
    assert len(seeded) == 8
    for info in LICENSE_CATALOG:
        assert seeded[info.code.value] == (
            info.allows_commercial,
            info.allows_derivatives,
            info.requires_sharealike,
            info.restrictiveness,
        ), f"la semilla de {info.code.value} no coincide con el dominio"


async def test_hnsw_and_partial_indexes_exist(conn: AsyncConnection) -> None:
    indexes = dict(
        (
            await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename='photos'")
            )
        ).all()
    )
    assert "hnsw" in indexes["ix_photos_embedding_hnsw"]
    assert "vector_cosine_ops" in indexes["ix_photos_embedding_hnsw"]
    assert "gist" in indexes["ix_photos_location_gist"]
    assert "WHERE" in indexes["ix_photos_ready_solved"]


async def test_storage_trigger_keeps_the_quota_in_sync(migrated_url: str) -> None:
    """``users.storage_used_bytes`` lo mantiene el trigger (``docs/data-model.md``)."""
    engine = create_async_engine(migrated_url)
    try:
        async with engine.begin() as conn:
            user_id = (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, display_name) VALUES "
                        "(:email, 'Trigger') RETURNING id"
                    ),
                    {"email": "trigger-quota@example.org"},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO photos (owner_id, s3_bucket, s3_key_original, "
                    "checksum_sha256, original_bytes) VALUES "
                    "(:uid, 'b', 'k', '\\x00'::bytea, 1000)"
                ),
                {"uid": user_id},
            )
            used = (
                await conn.execute(
                    text("SELECT storage_used_bytes FROM users WHERE id = :uid"),
                    {"uid": user_id},
                )
            ).scalar_one()
            assert used == 1000

            await conn.execute(
                text("UPDATE photos SET deleted_at = now() WHERE owner_id = :uid"),
                {"uid": user_id},
            )
            after = (
                await conn.execute(
                    text("SELECT storage_used_bytes FROM users WHERE id = :uid"),
                    {"uid": user_id},
                )
            ).scalar_one()
            assert after == 0

            # Limpieza: la base es compartida por el resto de tests del módulo.
            await conn.execute(text("DELETE FROM photos WHERE owner_id = :uid"), {"uid": user_id})
            await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
    finally:
        await engine.dispose()


async def test_audit_log_is_append_only(migrated_url: str) -> None:
    from sqlalchemy.exc import DBAPIError

    engine = create_async_engine(migrated_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO audit_log (action, entity_type) VALUES ('t', 'photo')")
            )
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM audit_log"))
    finally:
        await engine.dispose()


def test_downgrade_and_upgrade_round_trip(migrated_url: str) -> None:
    """Expand → migrate → contract exige que la bajada también funcione."""
    down = _run_alembic(migrated_url, "downgrade", "base")
    assert down.returncode == 0, f"alembic downgrade base falló:\n{down.stderr}"
    up = _run_alembic(migrated_url, "upgrade", "head")
    assert up.returncode == 0, f"alembic upgrade head falló tras bajar:\n{up.stderr}"
