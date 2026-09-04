"""La búsqueda por cono celeste: que use índice, y que devuelva lo mismo que antes.

Dos propiedades, y las dos hacen falta:

* **Rendimiento.** Sin un test que mire el plan, que la consulta central del
  producto vuelva a hacer un escaneo secuencial no lo nota nadie hasta que la tabla
  crece. Aquí se ejecuta ``EXPLAIN (ANALYZE, BUFFERS)`` sobre la consulta real y se
  falla si aparece un ``Seq Scan`` sobre ``photos``.
* **Equivalencia.** El cambio solo vale si no mueve ni una fila. Se compara contra
  el predicado anterior (``ST_DistanceSphere(...) <= radio``) sobre miles de fotos,
  incluidos los polos y el corte de RA 0/360, que son los casos donde un prefiltro
  por caja escrito a mano se equivoca.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.astro import angular_separation_deg
from app.models import Base
from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.models.user import User
from app.repositories.photo import METERS_PER_DEGREE, PhotoRepository
from app.schemas.search import PhotoSearchQuery
from tests.conftest import requires_postgis
from tests.integration.test_schema import _postgres_container_class

pytestmark = [pytest.mark.integration, requires_postgis]

#: Banda alrededor del radio donde el resultado es indecidible en coma flotante.
#: 1e-9 grados son 3.6 microsegundos de arco: muy por debajo de cualquier precisión
#: astrométrica real, así que excluirla no debilita el test.
BOUNDARY_TOLERANCE_DEG = 1e-9

#: Suficientes filas para que el planificador prefiera el índice a un escaneo.
SEEDED_PHOTOS = 6000

#: Conos de prueba: casos normales y los cuatro que rompen un prefiltro por caja.
CONES: list[tuple[str, float, float, float]] = [
    ("campo normal", 10.6847, 41.269, 2.0),
    ("campo normal ancho", 200.0, -30.0, 8.0),
    ("corte RA=0 por abajo", 0.5, 0.0, 3.0),
    ("corte RA=0 por arriba", 359.5, 0.0, 3.0),
    ("corte RA=0 exacto", 0.0, 12.0, 5.0),
    ("polo norte", 0.0, 90.0, 6.0),
    ("polo sur", 0.0, -90.0, 6.0),
    ("cerca del polo norte", 123.0, 87.0, 5.0),
    ("cerca del polo sur", 300.0, -87.0, 5.0),
    ("cono que engloba el polo", 45.0, 86.0, 10.0),
    ("radio grande", 120.0, -20.0, 45.0),
    ("hemisferio", 90.0, 0.0, 90.0),
]


def offset_position(ra: float, dec: float, sep_deg: float, pa_deg: float) -> tuple[float, float]:
    """Un punto a ``sep_deg`` exactos del centro, en el ángulo de posición ``pa_deg``.

    Fórmula estándar de desplazamiento sobre la esfera. Sirve para sembrar fotos a
    distancias **conocidas** del centro de cada cono y poder afirmar que las de
    dentro entran y las de fuera no, en vez de confiar en que el azar ponga alguna
    cerca.
    """
    ra_r, dec_r = math.radians(ra), math.radians(dec)
    sep, pa = math.radians(sep_deg), math.radians(pa_deg)
    dec2 = math.asin(
        max(
            -1.0,
            min(
                1.0,
                math.sin(dec_r) * math.cos(sep) + math.cos(dec_r) * math.sin(sep) * math.cos(pa),
            ),
        )
    )
    ra2 = ra_r + math.atan2(
        math.sin(pa) * math.sin(sep) * math.cos(dec_r),
        math.cos(sep) - math.sin(dec_r) * math.sin(dec2),
    )
    return math.degrees(ra2) % 360.0, math.degrees(dec2)


def controlled_points() -> list[tuple[float, float, bool]]:
    """``(ra, dec, dentro)`` alrededor de cada cono de :data:`CONES`.

    Cubre el interior, el borde por dentro y el exterior, en cuatro ángulos de
    posición. Con los polos y el corte de RA entre los centros, esto siembra
    exactamente los casos que un prefiltro por caja se come.
    """
    points: list[tuple[float, float, bool]] = []
    for _, ra, dec, radius in CONES:
        for pa in (0.0, 90.0, 180.0, 270.0):
            for factor, inside in ((0.1, True), (0.5, True), (0.95, True), (1.5, False)):
                new_ra, new_dec = offset_position(ra, dec, radius * factor, pa)
                points.append((new_ra, new_dec, inside))
    return points


def _seed_sql() -> str:
    """Fotos repartidas por todo el cielo, con densidad extra en los casos difíciles.

    El reparto en Dec usa ``asin`` para que sea uniforme **en área** y no en grados:
    si no, se acumularían en los polos y el test mediría un cielo que no existe.
    """
    return f"""
    INSERT INTO photos (
        owner_id, status, s3_bucket, s3_key_original, checksum_sha256,
        ra_deg, dec_deg, is_plate_solved, quality_score
    )
    SELECT
        :owner, 'ready'::photo_status, 'b', 'k' || g,
        decode(lpad(to_hex(g), 64, '0'), 'hex'),
        CASE
            WHEN g % 12 = 0 THEN (random() * 4.0)
            WHEN g % 12 = 1 THEN 356.0 + random() * 4.0
            ELSE random() * 360.0
        END,
        CASE
            WHEN g % 15 = 0 THEN 86.0 + random() * 4.0
            WHEN g % 15 = 1 THEN -90.0 + random() * 4.0
            ELSE degrees(asin(random() * 2.0 - 1.0))
        END,
        true, random()
    FROM generate_series(1, {SEEDED_PHOTOS}) g;
    """


@pytest.fixture(scope="module")
def seeded() -> Iterator[str]:
    """Una base con el esquema y unos miles de fotos. Vive todo el módulo.

    La fixture es **síncrona** a propósito: sembrar 6000 fotos una vez por test
    sería absurdo, y una fixture async de ámbito módulo obligaría a atar todos los
    tests al mismo bucle de eventos. Con ``asyncio.run`` el montaje queda aislado.
    """
    import asyncio
    import os

    url = os.environ.get("DATABASE_URL_TEST")
    container = None
    if url is None:
        container = _postgres_container_class()("postgis/postgis:16-3.4", driver="asyncpg")
        container.start()
        url = str(container.get_connection_url())

    async def build(database_url: str) -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                for extension in ("postgis", "pgcrypto", "citext", "vector"):
                    await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                owner = User(email=f"cone-{uuid.uuid4().hex[:8]}@example.org", display_name="Cono")
                session.add(owner)
                await session.flush()
                await session.execute(text(_seed_sql()), {"owner": owner.id})
                # Puntos a distancias conocidas de cada cono: sin esto, un cono de
                # 2° sobre 6000 fotos repartidas por todo el cielo sale vacío casi
                # siempre y el caso no demuestra nada.
                for index, (ra, dec, _inside) in enumerate(controlled_points()):
                    session.add(
                        Photo(
                            owner_id=owner.id,
                            status=PhotoStatus.READY,
                            s3_bucket="b",
                            s3_key_original=f"controlled-{index}",
                            checksum_sha256=(f"c{index}".encode().ljust(32, b"\x00")),
                            ra_deg=ra,
                            dec_deg=dec,
                            is_plate_solved=True,
                            quality_score=0.5,
                        )
                    )
                await session.commit()
            async with engine.begin() as conn:
                # Sin estadísticas el planificador no sabe qué le conviene.
                await conn.execute(text("ANALYZE photos"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(build(url))
        yield url
    finally:
        if container is not None:
            container.stop()


@pytest_asyncio.fixture
async def session(seeded: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(seeded)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


def _cone_query(ra: float, dec: float, radius: float) -> PhotoSearchQuery:
    return PhotoSearchQuery.model_validate({"ra": ra, "dec": dec, "radius": radius, "limit": 200})


def _compiled(query: PhotoSearchQuery) -> str:
    """El SQL literal de la consulta real del repositorio, sin inventar nada."""
    from sqlalchemy.dialects import postgresql

    stmt = PhotoRepository._apply_filters(
        PhotoRepository, PhotoRepository._base_select(), query, None
    )
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------- #
# El índice existe y se usa
# --------------------------------------------------------------------------- #
async def test_the_sky_index_exists(session: AsyncSession) -> None:
    definition = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_photos_sky_gist'")
        )
    ).scalar_one_or_none()
    assert definition is not None, "falta el índice del cono celeste"
    assert "gist" in definition
    assert "geography" in definition


@pytest.mark.parametrize(("label", "ra", "dec", "radius"), CONES, ids=[c[0] for c in CONES])
async def test_the_cone_search_never_falls_back_to_a_seq_scan(
    session: AsyncSession, label: str, ra: float, dec: float, radius: float
) -> None:
    """El test que impide que esto se rompa otra vez en silencio.

    Cubre también los polos y el corte de RA: si alguna vez alguien sustituye la
    expresión de la consulta por otra que no encaje con la del índice, PostgreSQL no
    avisa — simplemente vuelve a escanear la tabla entera. Esto sí avisa.
    """
    plan = (
        await session.execute(
            text(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _compiled(_cone_query(ra, dec, radius))
            )
        )
    ).scalar_one()
    rendered = str(plan)

    assert "Seq Scan" not in rendered, (
        f"La búsqueda por cono «{label}» (ra={ra}, dec={dec}, r={radius}) hace un "
        f"escaneo secuencial de `photos`. El índice ix_photos_sky_gist no se está "
        f"usando; comprueba que la expresión de la consulta sigue siendo idéntica a "
        f"la del índice.\n{rendered[:1500]}"
    )
    assert "ix_photos_sky_gist" in rendered, (
        f"«{label}» no usa el índice del cielo:\n{rendered[:1500]}"
    )


# --------------------------------------------------------------------------- #
# Equivalencia con la consulta anterior
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("label", "ra", "dec", "radius"), CONES, ids=[c[0] for c in CONES])
async def test_the_indexed_cone_returns_exactly_the_same_rows_as_before(
    session: AsyncSession, label: str, ra: float, dec: float, radius: float
) -> None:
    """El predicado nuevo no puede mover ni una fila respecto del anterior.

    La consulta vieja usaba ``ST_DistanceSphere(...) <= radio``; aquí se ejecuta tal
    cual, sin índice posible, y se compara el conjunto de ids con el que devuelve el
    repositorio. Es la única forma de demostrar que el índice no se come nada.
    """
    # Los valores van literales y no como parámetros: asyncpg no sabe inferir el
    # tipo de `$1 * $2` entre dos floats sin contexto, y aquí no hay entrada de
    # usuario que escapar.
    old_sql = f"""
        SELECT id FROM photos
        WHERE deleted_at IS NULL AND status = 'ready'
          AND ra_deg IS NOT NULL AND dec_deg IS NOT NULL
          AND ST_DistanceSphere(
                ST_SetSRID(ST_MakePoint(ra_deg - 180.0, dec_deg), 4326),
                ST_SetSRID(ST_MakePoint({ra} - 180.0, {dec}), 4326)
              ) <= {radius} * {METERS_PER_DEGREE}
    """
    old = set((await session.execute(text(old_sql))).scalars().all())

    new = set(
        (
            await session.execute(
                text("SELECT id FROM (" + _compiled(_cone_query(ra, dec, radius)) + ") AS q")
            )
        )
        .scalars()
        .all()
    )

    assert new == old, (
        f"«{label}» difiere del predicado anterior: "
        f"{len(old - new)} fotos perdidas, {len(new - old)} de más."
    )
    # Garantizado por los puntos controlados: 12 dentro por cono como mínimo.
    assert len(old) >= 12, (
        f"«{label}» solo seleccionó {len(old)} fotos; el caso no está probando el cono de verdad."
    )


async def test_the_cone_agrees_with_the_angular_separation_formula(
    session: AsyncSession,
) -> None:
    """Contraste independiente: la separación angular calculada en Python.

    Los dos predicados SQL podrían coincidir entre sí y estar los dos mal. Esto los
    contrasta contra ``domain.astro.angular_separation_deg``, que no sabe nada de
    PostGIS.
    """
    from app.domain.astro import angular_separation_deg

    ra, dec, radius = 47.5, -12.25, 4.0
    rows = (
        await session.execute(
            text(
                "SELECT id, ra_deg, dec_deg FROM photos "
                "WHERE ra_deg IS NOT NULL AND dec_deg IS NOT NULL"
            )
        )
    ).all()
    expected = {row[0] for row in rows if angular_separation_deg(row[1], row[2], ra, dec) <= radius}
    got = set(
        (
            await session.execute(
                text("SELECT id FROM (" + _compiled(_cone_query(ra, dec, radius)) + ") AS q")
            )
        )
        .scalars()
        .all()
    )
    # Las fotos justo en el borde pueden caer de un lado u otro por redondeo; se
    # exige coincidencia salvo en una banda de un microgrado alrededor del radio.
    disputed = {
        row[0]
        for row in rows
        if abs(angular_separation_deg(row[1], row[2], ra, dec) - radius) < 1e-6
    }
    assert (got - disputed) == (expected - disputed)
    assert expected, "el caso de prueba no seleccionó ninguna foto"


async def test_a_cone_at_the_pole_sees_every_right_ascension(
    session: AsyncSession,
) -> None:
    """En el polo la RA es degenerada: un cono de 5° alrededor del polo debe coger
    fotos de todas las ascensiones rectas, no solo de una franja."""
    ids_and_ra = (
        (
            await session.execute(
                text("SELECT ra_deg FROM (" + _compiled(_cone_query(0.0, 90.0, 5.0)) + ") AS q")
            )
        )
        .scalars()
        .all()
    )
    assert len(ids_and_ra) > 20, "muy pocas fotos cerca del polo para que el test valga"
    # Las RA encontradas deben repartirse por los cuatro cuadrantes.
    quadrants = {int(ra // 90) for ra in ids_and_ra if ra is not None}
    assert quadrants == {0, 1, 2, 3}, (
        f"un cono polar solo encontró fotos en los cuadrantes {sorted(quadrants)}: "
        "el filtro está tratando la RA como si no fuera degenerada en el polo."
    )


async def test_photos_without_coordinates_are_never_returned(
    session: AsyncSession,
) -> None:
    """El índice es parcial; las fotos sin resolver no pueden colarse por el borde."""
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM ("
                + _compiled(_cone_query(90.0, 0.0, 90.0))
                + ") AS q WHERE ra_deg IS NULL OR dec_deg IS NULL"
            )
        )
    ).scalar_one()
    assert count == 0


def test_the_meters_per_degree_constant_matches_the_sphere_postgis_uses() -> None:
    """``METERS_PER_DEGREE`` tiene que ser el arco de un grado en *esa* esfera.

    PostGIS usa radio 6 371 008.7714 m para ``geography`` sin esferoide. Si esta
    constante se separara de ahí, el radio pedido y el aplicado dejarían de
    coincidir y nadie lo notaría salvo por resultados sutilmente mal.
    """
    postgis_sphere_radius_m = 6_371_008.7714
    exact = postgis_sphere_radius_m * math.pi / 180.0
    assert pytest.approx(exact, rel=1e-5) == METERS_PER_DEGREE


@pytest.mark.parametrize(("label", "ra", "dec", "radius"), CONES, ids=[c[0] for c in CONES])
async def test_points_at_known_distances_land_on_the_right_side(
    session: AsyncSession, label: str, ra: float, dec: float, radius: float
) -> None:
    """La prueba directa: sembramos a distancias exactas y exigimos el borde correcto.

    Los tests de equivalencia demuestran que la consulta nueva hace lo mismo que la
    vieja; este demuestra que lo que hacen **es lo correcto**, sin depender de que
    ninguna de las dos lo estuviera. Cubre los polos y el corte de RA, que es donde
    un prefiltro por caja escrito a mano se equivoca en silencio.
    """
    found = set(
        (
            await session.execute(
                text("SELECT id FROM (" + _compiled(_cone_query(ra, dec, radius)) + ") AS q")
            )
        )
        .scalars()
        .all()
    )
    keys = dict(
        (
            await session.execute(
                text(
                    "SELECT s3_key_original, id FROM photos "
                    "WHERE s3_key_original LIKE 'controlled-%'"
                )
            )
        ).all()
    )

    inside_missing: list[str] = []
    outside_leaked: list[str] = []
    for index, (point_ra, point_dec, inside) in enumerate(controlled_points()):
        separation = angular_separation_deg(point_ra, point_dec, ra, dec)
        # Un punto exactamente en el borde cae de un lado u otro según el último bit
        # del cálculo, y hay puntos sembrados para un cono que quedan a exactamente
        # el radio de otro (el polo está a 90° justos de cualquier punto del
        # ecuador). Esa banda es indecidible por definición y no se juzga.
        if abs(separation - radius) < BOUNDARY_TOLERANCE_DEG:
            continue
        # Solo interesan los puntos sembrados para *este* cono; los de los otros
        # centros caen donde caigan.
        if inside and separation > radius:
            continue
        if not inside and separation <= radius:
            continue
        photo_id = keys.get(f"controlled-{index}")
        if photo_id is None:
            continue
        if separation <= radius and photo_id not in found:
            inside_missing.append(f"sep={separation:.4f}° <= r={radius}")
        if separation > radius and photo_id in found:
            outside_leaked.append(f"sep={separation:.4f}° > r={radius}")

    assert not inside_missing, (
        f"«{label}» se dejó fuera fotos que están dentro del cono: {inside_missing[:5]}"
    )
    assert not outside_leaked, f"«{label}» devolvió fotos de fuera del cono: {outside_leaked[:5]}"
