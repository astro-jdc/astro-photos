"""Integración contra PostGIS real.

Se salta entero si no hay contenedores ni ``DATABASE_URL_TEST``: preferimos un
`skip` explícito a una suite roja que la gente aprende a ignorar.

Para correrlos en local:

.. code-block:: bash

    make up
    createdb -h localhost -U astro astrophotos_test   # o psql CREATE DATABASE
    DATABASE_URL_TEST=postgresql+asyncpg://astro:astro@localhost:5432/astrophotos_test \\
        backend/.venv/bin/pytest backend/tests/integration -q
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.licensing import LicenseCode
from app.domain.location import LocationPrecision
from app.models import Base
from app.models.enums import ObjectCatalog, ObjectType, PhotoStatus
from app.models.photo import Photo
from app.models.sky_object import SkyObject
from app.models.user import User
from tests.conftest import requires_postgis

pytestmark = [pytest.mark.integration, requires_postgis]


def _postgres_container_class() -> type:
    """El módulo de testcontainers cambió de sitio; se aceptan las dos ubicaciones."""
    import importlib

    for module in ("testcontainers.community.postgres", "testcontainers.postgres"):
        try:
            return importlib.import_module(module).PostgresContainer  # type: ignore[no-any-return]
        except ImportError:
            continue
    pytest.skip("testcontainers no está disponible")


@pytest_asyncio.fixture
async def session(integration_database_url: str | None) -> AsyncIterator[AsyncSession]:
    """Sesión contra una base efímera con el esquema creado desde la metadata.

    Se usa ``create_all`` y no Alembic a propósito: así este test comprueba que los
    **modelos** son coherentes, y el test de migración comprueba que la migración
    coincide con ellos.
    """
    url = integration_database_url
    container = None
    if url is None:
        container_cls = _postgres_container_class()
        container = container_cls("postgis/postgis:16-3.4", driver="asyncpg")
        container.start()
        url = container.get_connection_url()

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for extension in ("postgis", "pgcrypto", "citext", "vector"):
                await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            yield db
    finally:
        await engine.dispose()
        if container is not None:
            container.stop()


async def _user(session: AsyncSession) -> User:
    user = User(
        email=f"test-{uuid.uuid4().hex[:8]}@example.org",
        display_name="Observador",
    )
    session.add(user)
    await session.flush()
    return user


async def test_extensions_are_available(session: AsyncSession) -> None:
    rows = (
        (await session.execute(text("SELECT extname FROM pg_extension ORDER BY extname")))
        .scalars()
        .all()
    )
    assert {"postgis", "vector", "citext", "pgcrypto"} <= set(rows)


async def test_all_tables_of_the_data_model_exist(session: AsyncSession) -> None:
    rows = set(
        (await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")))
        .scalars()
        .all()
    )
    expected = {
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
    }
    assert expected <= rows


async def test_geography_column_and_gist_index_exist(session: AsyncSession) -> None:
    kind = (
        await session.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'photos' AND column_name = 'location'"
            )
        )
    ).scalar_one()
    assert kind == "geography"
    indexes = set(
        (await session.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'photos'")))
        .scalars()
        .all()
    )
    assert "ix_photos_location_gist" in indexes


async def test_embedding_column_is_a_768_dim_vector(session: AsyncSession) -> None:
    dimension = (
        await session.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'photos'::regclass AND attname = 'embedding'"
            )
        )
    ).scalar_one()
    assert dimension == 768


async def test_checksum_is_unique_per_owner(session: AsyncSession) -> None:
    """Deduplicación: el mismo fichero no se sube dos veces por el mismo usuario."""
    from sqlalchemy.exc import IntegrityError

    user = await _user(session)
    checksum = b"\x01" * 32
    for _ in range(2):
        session.add(
            Photo(
                owner_id=user.id,
                s3_bucket="b",
                s3_key_original="k",
                checksum_sha256=checksum,
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_postgis_distance_query_works(session: AsyncSession) -> None:
    """La consulta real de ``?near=...&km=...``."""
    user = await _user(session)
    teide = Photo(
        owner_id=user.id,
        status=PhotoStatus.READY,
        s3_bucket="b",
        s3_key_original="teide",
        checksum_sha256=b"\x02" * 32,
        location="SRID=4326;POINT(-16.5117 28.3005)",
        location_precision=LocationPrecision.EXACT,
    )
    tokyo = Photo(
        owner_id=user.id,
        status=PhotoStatus.READY,
        s3_bucket="b",
        s3_key_original="tokyo",
        checksum_sha256=b"\x03" * 32,
        location="SRID=4326;POINT(139.6503 35.6762)",
        location_precision=LocationPrecision.EXACT,
    )
    session.add_all([teide, tokyo])
    await session.flush()

    near = (
        (
            await session.execute(
                text(
                    "SELECT s3_key_original FROM photos WHERE ST_DWithin("
                    "location, ST_GeographyFromText('SRID=4326;POINT(-16.5 28.3)'), 50000)"
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(near) == ["teide"]
    await session.rollback()


async def test_quality_score_check_constraint_is_enforced(session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    user = await _user(session)
    session.add(
        Photo(
            owner_id=user.id,
            s3_bucket="b",
            s3_key_original="bad",
            checksum_sha256=b"\x04" * 32,
            quality_score=1.5,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_license_enum_rejects_unknown_codes(session: AsyncSession) -> None:
    with pytest.raises(Exception, match=r"invalid input value|InvalidTextRepresentation"):
        await session.execute(text("SELECT 'CC-BY-99.0'::license_code"))
    await session.rollback()


async def test_sky_object_designation_roundtrips(session: AsyncSession) -> None:
    obj = SkyObject(
        catalog=ObjectCatalog.MESSIER,
        catalog_number="31",
        common_name="Andromeda Galaxy",
        common_name_es="Galaxia de Andrómeda",
        object_type=ObjectType.GALAXY,
        ra_deg=10.6847,
        dec_deg=41.269,
        aliases=["M31", "NGC 224", "Andromeda"],
    )
    session.add(obj)
    await session.flush()
    assert obj.designation == "M31"
    await session.rollback()


async def test_photo_defaults_match_the_documented_ones(session: AsyncSession) -> None:
    user = await _user(session)
    photo = Photo(
        owner_id=user.id,
        s3_bucket="b",
        s3_key_original="defaults",
        checksum_sha256=b"\x05" * 32,
    )
    session.add(photo)
    await session.flush()
    await session.refresh(photo)
    assert photo.license is LicenseCode.CC_BY_NC
    assert photo.status is PhotoStatus.UPLOADING
    assert photo.allow_ai_training is True
    assert photo.allow_derivatives_in_stacks is True
    assert photo.location_precision is LocationPrecision.EXACT
    assert photo.created_at <= datetime.now(UTC)
    await session.rollback()
