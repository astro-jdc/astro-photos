"""Ofuscación de ubicación. Función pura; se aplica en el **serializador**.

Regla 5 de ``.claude/agents/backend-dev.md``:

===========  ==========================================================
precisión    qué se publica
===========  ==========================================================
``exact``    coordenadas tal cual
``city``     redondeo a 0.1° (~11 km en latitud)
``country``  centroide del país
``hidden``   ``null``
===========  ==========================================================

Se hace en la serialización y **no** en la consulta a propósito: las búsquedas
geoespaciales ("fotos de M31 a menos de 500 km de aquí") tienen que seguir usando
la posición real, y el pipeline de reconstrucción necesita la posición exacta para
calcular masa de aire y rotación de campo. Lo que se protege es lo que **sale por
la API**, y hay un test que lo comprueba endpoint por endpoint.

La altitud se degrada con la misma escalera: un `elevation_m` de 2 390 m junto a un
redondeo de ciudad identifica un observatorio concreto.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "COUNTRY_CENTROIDS",
    "GeoPoint",
    "LocationPrecision",
    "ObfuscatedLocation",
    "obfuscate_location",
]


class LocationPrecision(StrEnum):
    """Valores del enum de Postgres ``location_precision`` (``docs/data-model.md``)."""

    EXACT = "exact"
    CITY = "city"
    COUNTRY = "country"
    HIDDEN = "hidden"


#: Tamaño de celda del redondeo de ciudad, en grados.
CITY_GRID_DEG = 0.1

#: Metros por grado de latitud (esfera de radio medio). Sirve para reportar una
#: `accuracy_m` honesta tras el redondeo, en vez de mentir con la original.
METERS_PER_DEGREE_LAT = 111_320.0

#: Escalón al que se redondea la altitud con precisión ``city``, en metros.
CITY_ELEVATION_STEP_M = 100.0


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """Posición del observador tal como está en la base de datos.

    ``country_code`` (ISO 3166-1 alfa-2) lo rellena el worker de ingesta por
    geocodificación inversa; es ``None`` mientras no se haya resuelto.
    """

    lat: float
    lon: float
    accuracy_m: float | None = None
    elevation_m: float | None = None
    country_code: str | None = None


@dataclass(frozen=True, slots=True)
class ObfuscatedLocation:
    """Lo que sale por la API. Todos los campos pueden ser ``None``."""

    lat: float | None
    lon: float | None
    accuracy_m: float | None
    elevation_m: float | None
    precision: LocationPrecision
    #: Código de país cuando se publica a esa granularidad; ``None`` en otro caso.
    country_code: str | None = None


#: Centroides aproximados (grados) de los países con más actividad astrofotográfica.
#: No es un dataset geográfico: es una tabla de presentación. Si un país no está
#: aquí, ``country`` degrada a ``hidden`` — la opción conservadora es no publicar
#: nada, nunca inventar un punto.
COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    "AR": (-35.38, -65.17),
    "AT": (47.68, 13.35),
    "AU": (-25.73, 134.49),
    "BE": (50.64, 4.64),
    "BR": (-10.79, -53.09),
    "CA": (61.36, -98.31),
    "CH": (46.80, 8.21),
    "CL": (-37.73, -71.38),
    "CN": (36.56, 103.82),
    "CZ": (49.73, 15.31),
    "DE": (51.11, 10.39),
    "DK": (55.97, 10.03),
    "ES": (40.24, -3.65),
    "FI": (64.50, 26.27),
    "FR": (46.56, 2.34),
    "GB": (54.12, -2.86),
    "GR": (39.07, 22.96),
    "HU": (47.16, 19.40),
    "IE": (53.18, -8.14),
    "IN": (22.89, 79.61),
    "IT": (42.80, 12.07),
    "JP": (37.59, 138.03),
    "MX": (23.95, -102.52),
    "NL": (52.11, 5.28),
    "NO": (61.15, 8.79),
    "NZ": (-41.81, 171.48),
    "PL": (52.13, 19.39),
    "PT": (39.60, -8.50),
    "RO": (45.85, 24.97),
    "SE": (62.78, 16.75),
    "TR": (39.06, 35.16),
    "US": (45.68, -112.46),
    "ZA": (-29.00, 25.08),
}


def _round_to_grid(value: float, step: float) -> float:
    """Redondeo al múltiplo de ``step`` más cercano, sin arrastre de coma flotante."""
    return round(round(value / step) * step, 6)


def obfuscate_location(
    point: GeoPoint | None,
    precision: LocationPrecision | str,
) -> ObfuscatedLocation | None:
    """Degrada ``point`` al nivel que el autor autorizó.

    Devuelve ``None`` cuando no hay nada publicable (sin punto, o ``hidden``, o
    ``country`` sin país conocido): el serializador emite entonces ``location: null``.

    Es idempotente para ``exact`` y monótona: aplicarla dos veces con la misma
    precisión da el mismo resultado, y una precisión más gruesa nunca revela más
    que una más fina.
    """
    prec = LocationPrecision(precision)

    if point is None or prec is LocationPrecision.HIDDEN:
        return None

    if prec is LocationPrecision.EXACT:
        return ObfuscatedLocation(
            lat=point.lat,
            lon=point.lon,
            accuracy_m=point.accuracy_m,
            elevation_m=point.elevation_m,
            precision=prec,
            country_code=point.country_code,
        )

    if prec is LocationPrecision.CITY:
        # Media celda es el peor error posible tras el redondeo; se suma en
        # cuadratura con la incertidumbre original para no subestimarla.
        half_cell_m = CITY_GRID_DEG * METERS_PER_DEGREE_LAT / 2.0
        original = point.accuracy_m or 0.0
        accuracy = (half_cell_m**2 + original**2) ** 0.5
        elevation = (
            None
            if point.elevation_m is None
            else _round_to_grid(point.elevation_m, CITY_ELEVATION_STEP_M)
        )
        return ObfuscatedLocation(
            lat=_round_to_grid(point.lat, CITY_GRID_DEG),
            lon=_round_to_grid(point.lon, CITY_GRID_DEG),
            accuracy_m=round(accuracy, 1),
            elevation_m=elevation,
            precision=prec,
            country_code=point.country_code,
        )

    # LocationPrecision.COUNTRY
    code = (point.country_code or "").upper()
    centroid = COUNTRY_CENTROIDS.get(code)
    if centroid is None:
        # País desconocido: no se inventa un punto, se oculta.
        return None
    lat, lon = centroid
    return ObfuscatedLocation(
        lat=lat,
        lon=lon,
        # Un centroide nacional no tiene "precisión" en ningún sentido útil; se
        # publica None en vez de un número que invitaría a tratarlo como una medida.
        accuracy_m=None,
        elevation_m=None,
        precision=prec,
        country_code=code,
    )
