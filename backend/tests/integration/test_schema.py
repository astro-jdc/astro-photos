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


async def test_new_multipart_and_map_columns_exist(session: AsyncSession) -> None:
    """Columnas de la migración 0002, necesarias para `complete-multipart` y `result`."""
    columns = set(
        (
            await session.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE table_name IN ('photos', 'reconstructions')"
                )
            )
        )
        .scalars()
        .all()
    )
    assert "photos.multipart_upload_id" in columns
    assert "reconstructions.s3_key_uncertainty" in columns
    assert "reconstructions.s3_key_weight_map" in columns


async def test_stats_query_runs_and_counts_only_public_rows(
    session: AsyncSession,
) -> None:
    """La consulta real de ``GET /stats``, contra Postgres.

    Es SQL con agregados y `DISTINCT`: probarlo solo con dobles no demostraría nada.
    """
    from app.repositories.stats import StatsRepository

    alice = await _user(session)
    bob = await _user(session)

    def photo(owner: uuid.UUID, key: str, **kw: object) -> Photo:
        return Photo(
            owner_id=owner,
            s3_bucket="b",
            s3_key_original=key,
            checksum_sha256=key.encode().ljust(32, b"\x00")[:32],
            **kw,  # type: ignore[arg-type]
        )

    session.add_all(
        [
            # Cuentan: listas, sin borrar.
            photo(alice.id, "ready-1", status=PhotoStatus.READY, exposure_seconds=120.0),
            photo(alice.id, "ready-2", status=PhotoStatus.READY, exposure_seconds=300.0),
            photo(bob.id, "ready-3", status=PhotoStatus.READY, exposure_seconds=60.0),
            # No cuentan.
            photo(bob.id, "processing", status=PhotoStatus.PROCESSING, exposure_seconds=999.0),
            photo(
                bob.id,
                "deleted",
                status=PhotoStatus.READY,
                exposure_seconds=999.0,
                deleted_at=datetime.now(UTC),
            ),
        ]
    )
    await session.flush()

    stats = await StatsRepository(session).snapshot()
    assert stats.photo_count == 3
    assert stats.contributor_count == 2  # alice y bob, no tres filas
    assert stats.total_exposure_seconds == pytest.approx(480.0)
    assert stats.reconstruction_count == 0
    await session.rollback()


async def test_stats_query_survives_an_empty_database(session: AsyncSession) -> None:
    """La portada tiene que poder pintarse el primer día, sin dividir por cero."""
    from app.repositories.stats import StatsRepository

    stats = await StatsRepository(session).snapshot()
    assert stats.photo_count == 0
    assert stats.contributor_count == 0
    assert stats.total_exposure_seconds == 0.0


async def test_coverage_and_raw_sites_queries_run_against_postgis(
    session: AsyncSession,
) -> None:
    """El histograma y ``raw_sites``, contra Postgres de verdad.

    Son SQL con PostGIS (`ST_Y`), `width_bucket` y agregados: probarlos solo con
    dobles no demostraría que la consulta existe siquiera.
    """
    from app.domain.location import LocationPrecision
    from app.repositories.sky_object import SkyObjectRepository
    from app.services.sky_object import build_sites

    user = await _user(session)
    obj = SkyObject(
        catalog=ObjectCatalog.MESSIER,
        catalog_number="42",
        common_name="Orion Nebula",
        object_type=ObjectType.NEBULA,
        ra_deg=83.822,
        dec_deg=-5.391,
    )
    session.add(obj)
    await session.flush()

    def shot(key: str, lat: float, lon: float, precision: str, focal: float) -> Photo:
        return Photo(
            owner_id=user.id,
            status=PhotoStatus.READY,
            s3_bucket="b",
            s3_key_original=key,
            checksum_sha256=key.encode().ljust(32, b"\x00")[:32],
            object_id=obj.id,
            location=f"SRID=4326;POINT({lon} {lat})",
            location_precision=LocationPrecision(precision),
            country_code="ES",
            captured_at_utc=datetime(2026, 2, 14, 3, tzinfo=UTC),
            focal_length_mm=focal,
            quality_score=0.8,
        )

    session.add_all(
        [
            shot("north-exact", 40.4, -3.7, "exact", 200.0),
            shot("north-city", 40.41, -3.71, "city", 600.0),
            shot("south-hidden", -33.9, 18.4, "hidden", 200.0),
        ]
    )
    await session.flush()

    repo = SkyObjectRepository(session)
    cells = await repo.coverage(obj.id)
    assert cells, "el histograma no devolvió ninguna celda"
    assert all(c.period == "2026-02" for c in cells)
    # La foto oculta cuenta, pero sin latitud: una banda de 15° sigue siendo una
    # posición y su autor no autorizó publicarla.
    assert {c.lat_bin for c in cells} == {30, -999}
    assert all(c.best_quality == pytest.approx(0.8) for c in cells)

    raw = await repo.raw_sites(obj.id)
    # La oculta se excluye ya en SQL: no hay agregación en la que deba contribuir.
    assert len(raw) == 2
    assert LocationPrecision.HIDDEN not in {r.precision for r in raw}

    sites = build_sites(raw)
    assert sum(s.count for s in sites) == 2
    # La exacta y la de ciudad caen en el mismo punto publicado tras redondear, así
    # que se funden — y el punto queda etiquetado con la precisión más gruesa, que es
    # la única afirmación sostenible sobre él.
    assert len(sites) == 1
    assert sites[0].precision == "city"
    assert sites[0].lat == pytest.approx(40.4)
    await session.rollback()
