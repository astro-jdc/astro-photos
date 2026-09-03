"""``sites[]`` del mapa de cobertura: ofuscar **antes** de agregar.

El orden es toda la garantía. Si se agregara primero y se ofuscara el resultado,
bastaría con que una sola foto exacta cayera en el grupo para que el punto publicado
delatase el sitio — que es justo el fallo que la regla de privacidad existe para
evitar. Estos tests fijan ese orden.
"""

from __future__ import annotations

import pytest

from app.domain.location import COUNTRY_CENTROIDS, LocationPrecision
from app.repositories.sky_object import RawSiteRow
from app.services.sky_object import build_sites

#: Observatorio del Teide, con su topónimo real.
TEIDE_LAT, TEIDE_LON = 28.3005, -16.5117


def row(
    precision: LocationPrecision,
    *,
    lat: float = TEIDE_LAT,
    lon: float = TEIDE_LON,
    count: int = 1,
    country: str | None = "ES",
) -> RawSiteRow:
    return RawSiteRow(
        lat=lat,
        lon=lon,
        precision=precision,
        country_code=country,
        accuracy_m=10.0,
        elevation_m=2390.0,
        count=count,
    )


# --------------------------------------------------------------------------- #
# Cada precisión publica lo suyo
# --------------------------------------------------------------------------- #
def test_an_exact_site_publishes_the_real_position() -> None:
    sites = build_sites([row(LocationPrecision.EXACT)])
    assert len(sites) == 1
    assert sites[0].lat == pytest.approx(TEIDE_LAT)
    assert sites[0].precision == "exact"


def test_a_city_site_is_rounded_to_a_tenth_of_a_degree() -> None:
    sites = build_sites([row(LocationPrecision.CITY)])
    assert sites[0].lat == pytest.approx(28.3)
    assert sites[0].lon == pytest.approx(-16.5)


def test_a_country_site_lands_on_the_centroid() -> None:
    sites = build_sites([row(LocationPrecision.COUNTRY)])
    assert (sites[0].lat, sites[0].lon) == COUNTRY_CENTROIDS["ES"]


def test_a_hidden_site_contributes_nothing_at_all() -> None:
    assert build_sites([row(LocationPrecision.HIDDEN)]) == []


def test_a_country_site_without_a_known_country_is_dropped() -> None:
    """No se inventa un centroide: se cae del mapa."""
    assert build_sites([row(LocationPrecision.COUNTRY, country=None)]) == []


# --------------------------------------------------------------------------- #
# La propiedad que pidió el contrato
# --------------------------------------------------------------------------- #
def test_no_published_point_is_finer_than_the_coarsest_photo_behind_it() -> None:
    """Ningún punto de ``sites[]`` es más preciso que la foto menos precisa que lo compone.

    Se mezclan cuatro fotos en la **misma** posición real con las cuatro precisiones.
    Cada una se publica según lo que autorizó su autor; ninguna arrastra a las otras
    a un nivel más fino, y la oculta no aparece en ningún sitio.
    """
    rows = [
        row(LocationPrecision.EXACT, count=1),
        row(LocationPrecision.CITY, count=2),
        row(LocationPrecision.COUNTRY, count=4),
        row(LocationPrecision.HIDDEN, count=8),
    ]
    sites = build_sites(rows)
    by_precision = {s.precision: s for s in sites}

    # La oculta no está en ninguna parte.
    assert sum(s.count for s in sites) == 1 + 2 + 4
    assert "hidden" not in by_precision

    # Y ningún punto de precisión gruesa cayó sobre las coordenadas exactas.
    exact_point = (pytest.approx(TEIDE_LAT), pytest.approx(TEIDE_LON))
    for site in sites:
        if site.precision != "exact":
            assert (site.lat, site.lon) != exact_point
        # El recuento de cada punto es solo el de las fotos de su propia precisión.
        assert site.count in (1, 2, 4)


def test_merging_two_precisions_keeps_the_coarsest_label() -> None:
    """Si dos grupos acaban en el mismo punto publicado, gana la etiqueta más gruesa.

    Es la única afirmación sostenible sobre ese punto: no se puede prometer más
    precisión de la que autorizó el más restrictivo de sus autores.
    """
    rows = [
        # Una foto ya en la rejilla de ciudad, publicada como exacta…
        row(LocationPrecision.EXACT, lat=28.3, lon=-16.5, count=1),
        # …y otra que al redondear cae en ese mismo punto.
        row(LocationPrecision.CITY, lat=28.3005, lon=-16.5117, count=5),
    ]
    sites = build_sites(rows)
    assert len(sites) == 1
    assert sites[0].count == 6
    assert sites[0].precision == "city"


def test_an_exact_photo_never_upgrades_a_coarse_group() -> None:
    """El fallo que esto previene: una foto exacta no debe afinar el punto del grupo."""
    rows = [
        row(LocationPrecision.COUNTRY, count=1),
        row(LocationPrecision.COUNTRY, lat=40.0, lon=-3.0, count=1),
        row(LocationPrecision.EXACT, count=1),
    ]
    sites = build_sites(rows)
    country_point = COUNTRY_CENTROIDS["ES"]
    country_sites = [s for s in sites if (s.lat, s.lon) == country_point]
    assert len(country_sites) == 1
    # Las dos de país se funden en el centroide y no arrastran a la exacta.
    assert country_sites[0].count == 2
    assert country_sites[0].precision == "country"


# --------------------------------------------------------------------------- #
# Agregación y determinismo
# --------------------------------------------------------------------------- #
def test_photos_from_different_places_stay_apart() -> None:
    rows = [
        row(LocationPrecision.CITY, lat=28.3, lon=-16.5, count=3),
        row(LocationPrecision.CITY, lat=-33.9, lon=18.4, count=2),
    ]
    sites = build_sites(rows)
    assert len(sites) == 2
    assert {s.count for s in sites} == {3, 2}


def test_nearby_city_photos_merge_into_one_point() -> None:
    rows = [
        row(LocationPrecision.CITY, lat=28.301, lon=-16.512, count=1),
        row(LocationPrecision.CITY, lat=28.297, lon=-16.508, count=1),
    ]
    sites = build_sites(rows)
    assert len(sites) == 1
    assert sites[0].count == 2


def test_the_output_order_is_stable() -> None:
    """El mismo objeto pinta siempre el mismo mapa (regla dura 3)."""
    rows = [
        row(LocationPrecision.CITY, lat=40.0, lon=-3.0),
        row(LocationPrecision.CITY, lat=28.3, lon=-16.5),
        row(LocationPrecision.CITY, lat=-33.9, lon=18.4),
    ]
    first = [(s.lat, s.lon) for s in build_sites(rows)]
    second = [(s.lat, s.lon) for s in build_sites(list(reversed(rows)))]
    assert first == second == sorted(first)


def test_an_empty_object_has_an_empty_map() -> None:
    assert build_sites([]) == []


def test_counts_are_preserved_exactly() -> None:
    rows = [row(LocationPrecision.CITY, lat=float(i), lon=0.0, count=i) for i in range(1, 6)]
    assert sum(s.count for s in build_sites(rows)) == 15
