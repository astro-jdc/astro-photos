"""Tests de ``app.domain.location``: las 4 precisiones, una por una.

La ofuscación es una promesa hecha al autor de la foto; si se rompe no hay forma de
deshacerlo, así que se testea con detalle.
"""

from __future__ import annotations

import pytest

from app.domain.location import (
    CITY_GRID_DEG,
    COUNTRY_CENTROIDS,
    GeoPoint,
    LocationPrecision,
    obfuscate_location,
)

#: Observatorio del Teide, con altitud e incertidumbre realistas.
TEIDE = GeoPoint(
    lat=28.3005,
    lon=-16.5117,
    accuracy_m=12.0,
    elevation_m=2390.0,
    country_code="ES",
)


# --------------------------------------------------------------------------- #
# exact
# --------------------------------------------------------------------------- #
def test_exact_publishes_the_coordinates_untouched() -> None:
    result = obfuscate_location(TEIDE, LocationPrecision.EXACT)
    assert result is not None
    assert result.lat == TEIDE.lat
    assert result.lon == TEIDE.lon
    assert result.accuracy_m == TEIDE.accuracy_m
    assert result.elevation_m == TEIDE.elevation_m
    assert result.precision is LocationPrecision.EXACT


def test_exact_is_idempotent() -> None:
    once = obfuscate_location(TEIDE, LocationPrecision.EXACT)
    assert once is not None
    twice = obfuscate_location(
        GeoPoint(lat=once.lat or 0.0, lon=once.lon or 0.0), LocationPrecision.EXACT
    )
    assert twice is not None
    assert (twice.lat, twice.lon) == (once.lat, once.lon)


# --------------------------------------------------------------------------- #
# city
# --------------------------------------------------------------------------- #
def test_city_rounds_to_one_tenth_of_a_degree() -> None:
    result = obfuscate_location(TEIDE, LocationPrecision.CITY)
    assert result is not None
    assert result.lat == pytest.approx(28.3)
    assert result.lon == pytest.approx(-16.5)


def test_city_rounding_lands_on_the_grid() -> None:
    for lat, lon in [(0.04, 0.06), (-0.04, -0.06), (45.67, -123.45), (89.99, 179.99)]:
        result = obfuscate_location(GeoPoint(lat=lat, lon=lon), LocationPrecision.CITY)
        assert result is not None
        assert result.lat is not None and result.lon is not None
        assert abs(result.lat / CITY_GRID_DEG - round(result.lat / CITY_GRID_DEG)) < 1e-6
        assert abs(result.lon / CITY_GRID_DEG - round(result.lon / CITY_GRID_DEG)) < 1e-6


def test_city_never_moves_the_point_more_than_half_a_cell() -> None:
    for lat, lon in [(28.3005, -16.5117), (-33.9249, 18.4241), (35.6762, 139.6503)]:
        result = obfuscate_location(GeoPoint(lat=lat, lon=lon), LocationPrecision.CITY)
        assert result is not None
        assert result.lat is not None and result.lon is not None
        assert abs(result.lat - lat) <= CITY_GRID_DEG / 2 + 1e-9
        assert abs(result.lon - lon) <= CITY_GRID_DEG / 2 + 1e-9


def test_city_reports_an_honest_accuracy_not_the_original_one() -> None:
    """Publicar 12 m junto a coordenadas redondeadas sería mentir."""
    result = obfuscate_location(TEIDE, LocationPrecision.CITY)
    assert result is not None
    assert result.accuracy_m is not None
    assert result.accuracy_m > 5000.0


def test_city_degrades_the_elevation_too() -> None:
    """2390 m junto a un redondeo de ciudad identificaría el observatorio."""
    result = obfuscate_location(TEIDE, LocationPrecision.CITY)
    assert result is not None
    assert result.elevation_m == pytest.approx(2400.0)


def test_city_is_idempotent() -> None:
    once = obfuscate_location(TEIDE, LocationPrecision.CITY)
    assert once is not None
    twice = obfuscate_location(
        GeoPoint(lat=once.lat or 0.0, lon=once.lon or 0.0), LocationPrecision.CITY
    )
    assert twice is not None
    assert (twice.lat, twice.lon) == (once.lat, once.lon)


# --------------------------------------------------------------------------- #
# country
# --------------------------------------------------------------------------- #
def test_country_publishes_the_centroid() -> None:
    result = obfuscate_location(TEIDE, LocationPrecision.COUNTRY)
    assert result is not None
    assert (result.lat, result.lon) == COUNTRY_CENTROIDS["ES"]
    assert result.country_code == "ES"


def test_country_drops_accuracy_and_elevation() -> None:
    result = obfuscate_location(TEIDE, LocationPrecision.COUNTRY)
    assert result is not None
    assert result.accuracy_m is None
    assert result.elevation_m is None


def test_country_accepts_a_lowercase_code() -> None:
    point = GeoPoint(lat=1.0, lon=2.0, country_code="es")
    result = obfuscate_location(point, LocationPrecision.COUNTRY)
    assert result is not None
    assert result.country_code == "ES"


def test_country_hides_everything_when_the_country_is_unknown() -> None:
    """No se inventa un punto: la opción conservadora es no publicar nada."""
    assert obfuscate_location(GeoPoint(lat=1.0, lon=2.0), LocationPrecision.COUNTRY) is None
    assert (
        obfuscate_location(GeoPoint(lat=1.0, lon=2.0, country_code="XX"), LocationPrecision.COUNTRY)
        is None
    )


def test_country_centroids_are_valid_coordinates() -> None:
    for code, (lat, lon) in COUNTRY_CENTROIDS.items():
        assert -90.0 <= lat <= 90.0, code
        assert -180.0 <= lon <= 180.0, code
        assert len(code) == 2 and code.isupper()


# --------------------------------------------------------------------------- #
# hidden
# --------------------------------------------------------------------------- #
def test_hidden_publishes_nothing() -> None:
    assert obfuscate_location(TEIDE, LocationPrecision.HIDDEN) is None


def test_missing_point_publishes_nothing_at_any_precision() -> None:
    for precision in LocationPrecision:
        assert obfuscate_location(None, precision) is None


# --------------------------------------------------------------------------- #
# Propiedades transversales
# --------------------------------------------------------------------------- #
def test_coarser_precision_never_reveals_more_than_a_finer_one() -> None:
    exact = obfuscate_location(TEIDE, LocationPrecision.EXACT)
    city = obfuscate_location(TEIDE, LocationPrecision.CITY)
    country = obfuscate_location(TEIDE, LocationPrecision.COUNTRY)
    hidden = obfuscate_location(TEIDE, LocationPrecision.HIDDEN)

    assert exact is not None and city is not None and country is not None
    assert hidden is None
    # La incertidumbre solo puede crecer al bajar de precisión.
    assert (exact.accuracy_m or 0.0) < (city.accuracy_m or 0.0)
    # Y la altitud desaparece por completo a nivel de país.
    assert exact.elevation_m is not None
    assert country.elevation_m is None


def test_precision_accepts_the_string_form_used_by_the_api() -> None:
    for value in ("exact", "city", "country", "hidden"):
        obfuscate_location(TEIDE, value)


def test_unknown_precision_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="not a valid LocationPrecision"):
        obfuscate_location(TEIDE, "approximate")


def test_the_four_precisions_of_the_data_model_are_the_only_ones() -> None:
    assert {p.value for p in LocationPrecision} == {"exact", "city", "country", "hidden"}
