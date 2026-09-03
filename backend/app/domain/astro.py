"""Cálculos astronómicos puros.

Sin IO, sin efemérides externas, sin base de datos: todo se resuelve con series
analíticas de baja precisión. La precisión de cada función está documentada en su
docstring, y es deliberadamente la que necesita el producto (ordenar y ponderar
frames), no la de un catálogo astrométrico.

Convenciones:

* Ángulos en **grados** salvo donde el nombre diga otra cosa.
* Instantes en ``datetime`` **timezone-aware**; se convierten a UTC internamente.
  Un ``datetime`` naive se rechaza: en este proyecto la ambigüedad horaria es un bug
  (``docs/data-model.md``: "todos los instantes se guardan en UTC").
* RA/Dec en el marco J2000 (los efectos de precesión, del orden de 0.01°/año, quedan
  por debajo de la precisión de estas series salvo en escalas de décadas).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

__all__ = [
    "ARCSEC_PER_RADIAN",
    "AirmassModel",
    "EquatorialCoord",
    "HorizontalCoord",
    "MoonState",
    "airmass",
    "alt_az",
    "angular_separation_deg",
    "diffraction_limit_arcsec",
    "julian_date",
    "moon_illumination",
    "moon_separation_deg",
    "moon_state",
    "pixel_scale_arcsec",
    "sampling_ratio",
]

#: 180 * 3600 / pi — conversión radianes → segundos de arco.
ARCSEC_PER_RADIAN: float = 206264.80624709636

#: Modelos de masa de aire soportados.
AirmassModel = Literal["pickering", "kasten-young"]


@dataclass(frozen=True, slots=True)
class EquatorialCoord:
    """Coordenadas ecuatoriales en grados."""

    ra_deg: float
    dec_deg: float


@dataclass(frozen=True, slots=True)
class HorizontalCoord:
    """Coordenadas horizontales en grados. ``az_deg`` se mide desde el norte hacia el este."""

    altitude_deg: float
    azimuth_deg: float


def _require_aware(when: datetime) -> datetime:
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError("Se requiere un datetime timezone-aware; los instantes se manejan en UTC.")
    return when.astimezone(UTC)


def julian_date(when: datetime) -> float:
    """Día juliano (TT≈UT, se ignora ΔT ≈ 70 s ⇒ error < 0.001° en posición lunar)."""
    dt = _require_aware(when)
    year, month = dt.year, dt.month
    day = dt.day + (dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60.0) / 60.0) / 24.0
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4  # calendario gregoriano
    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5


def _centuries_since_j2000(when: datetime) -> float:
    return (julian_date(when) - 2451545.0) / 36525.0


# --------------------------------------------------------------------------- #
# Masa de aire
# --------------------------------------------------------------------------- #
def airmass(altitude_deg: float, model: AirmassModel = "pickering") -> float:
    """Masa de aire para una altitud sobre el horizonte.

    No se usa ``sec(z)``: diverge por debajo de ~20° de altitud, justo donde la
    extinción y la dispersión cromática diferencial empiezan a importar
    (``docs/research/…`` §"Airmass"). Modelos disponibles:

    * ``pickering`` (por defecto) — Pickering (2002), *The Southern Sky Guide*:
      ``X = 1 / sin(h + 244 / (165 + 47·h^1.1))``. Error < 0.001 respecto a la
      integración de una atmósfera estándar hasta el horizonte. En el cenit da
      1.0000002.
    * ``kasten-young`` — Kasten & Young (1989):
      ``X = 1 / (sin h + 0.50572·(h + 6.07995)^-1.6364)``. Muy usado en el mundo
      solar; en el cenit da 0.9997 (sesgo conocido del ajuste).

    Devuelve ``inf`` para objetos en el horizonte o por debajo: un frame así no es
    utilizable y los consumidores deben descartarlo, no extrapolar.
    """
    if altitude_deg <= 0.0:
        return math.inf
    h = min(altitude_deg, 90.0)
    if model == "pickering":
        return 1.0 / math.sin(math.radians(h + 244.0 / (165.0 + 47.0 * h**1.1)))
    if model == "kasten-young":
        return 1.0 / (math.sin(math.radians(h)) + 0.50572 * (h + 6.07995) ** -1.6364)
    raise ValueError(f"Modelo de masa de aire desconocido: {model!r}")


# --------------------------------------------------------------------------- #
# Alt/Az
# --------------------------------------------------------------------------- #
def _gmst_deg(when: datetime) -> float:
    """Tiempo sidéreo medio de Greenwich en grados (Meeus 12.4). Precisión ~0.1 s."""
    jd = julian_date(when)
    t = (jd - 2451545.0) / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    )
    return gmst % 360.0


def alt_az(
    ra_deg: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
    when: datetime,
) -> HorizontalCoord:
    """Altitud y acimut **geométricos** (sin refracción) de un objeto ecuatorial.

    ``lon_deg`` positiva al este de Greenwich. El acimut se mide desde el norte
    hacia el este (0° = N, 90° = E), que es la convención de la mayoría de software
    de planetario.

    Precisión ≈ 0.01° para coordenadas J2000 en la época actual: se ignoran
    precesión, nutación, aberración, paralaje diurna y refracción atmosférica.
    Para la refracción cerca del horizonte usa un modelo aparte; para el peso de un
    frame no hace falta (:func:`airmass` ya absorbe la geometría).
    """
    lst = (_gmst_deg(when) + lon_deg) % 360.0
    hour_angle = math.radians((lst - ra_deg) % 360.0)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)

    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(hour_angle)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)

    # atan2 evita la degeneración de acos() cerca del meridiano y fija el cuadrante.
    az = math.atan2(
        -math.cos(dec) * math.sin(hour_angle),
        math.sin(dec) * math.cos(lat) - math.cos(dec) * math.sin(lat) * math.cos(hour_angle),
    )
    return HorizontalCoord(
        altitude_deg=math.degrees(alt),
        azimuth_deg=math.degrees(az) % 360.0,
    )


# --------------------------------------------------------------------------- #
# Separaciones angulares
# --------------------------------------------------------------------------- #
def angular_separation_deg(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """Separación angular entre dos direcciones del cielo.

    Fórmula de Vincenty sobre la esfera: numéricamente estable tanto para
    separaciones muy pequeñas (donde ``acos`` pierde todos los dígitos) como para
    las cercanas a 180°.
    """
    ra1, dec1 = math.radians(ra1_deg), math.radians(dec1_deg)
    ra2, dec2 = math.radians(ra2_deg), math.radians(dec2_deg)
    d_ra = ra2 - ra1
    num = math.hypot(
        math.cos(dec2) * math.sin(d_ra),
        math.cos(dec1) * math.sin(dec2) - math.sin(dec1) * math.cos(dec2) * math.cos(d_ra),
    )
    den = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(d_ra)
    return math.degrees(math.atan2(num, den))


# --------------------------------------------------------------------------- #
# Luna
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MoonState:
    """Estado geocéntrico aproximado de la Luna en un instante."""

    ra_deg: float
    dec_deg: float
    #: Longitud eclíptica aparente, grados.
    ecliptic_lon_deg: float
    #: Latitud eclíptica aparente, grados.
    ecliptic_lat_deg: float
    #: Distancia Tierra–Luna, km.
    distance_km: float
    #: Fracción iluminada del disco, 0 (luna nueva) – 1 (llena).
    illumination: float
    #: Ángulo de fase, grados (0 = llena, 180 = nueva).
    phase_angle_deg: float
    #: Elongación geocéntrica Sol–Luna, grados.
    elongation_deg: float


_AU_KM = 149597870.7


def _sun_apparent(t: float) -> tuple[float, float]:
    """Longitud eclíptica aparente del Sol (grados) y distancia (UA). Meeus cap. 25."""
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    e = 0.016708634 - 0.000042037 * t
    m_rad = math.radians(m)
    c = (
        (1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m_rad)
        + (0.019993 - 0.000101 * t) * math.sin(2 * m_rad)
        + 0.000289 * math.sin(3 * m_rad)
    )
    true_lon = (l0 + c) % 360.0
    nu = math.radians(m + c)
    radius_au = 1.000001018 * (1 - e * e) / (1 + e * math.cos(nu))
    return true_lon, radius_au


def _obliquity_deg(t: float) -> float:
    """Oblicuidad media de la eclíptica (Meeus 22.2), grados."""
    return 23.439291 - 0.0130042 * t - 1.64e-7 * t * t + 5.04e-7 * t**3


def moon_state(when: datetime) -> MoonState:
    """Posición y fase de la Luna con la serie truncada de Meeus (cap. 47).

    **Precisión:** ~0.2° en longitud, ~0.1° en latitud, ~1 % en distancia y
    ~0.5 puntos porcentuales en fracción iluminada. Es geocéntrica: la paralaje
    lunar (hasta 1°) no se corrige, porque para decidir "¿la Luna estropeó esta
    toma?" un grado es irrelevante frente a los ~30–90° que separan una buena
    toma de una mala. Para efemérides serias usa ``skyfield``/JPL en ``models/``.
    """
    t = _centuries_since_j2000(when)

    lp = 218.3164477 + 481267.88123421 * t  # longitud media
    d = 297.8501921 + 445267.1114034 * t  # elongación media
    m = 357.5291092 + 35999.0502909 * t  # anomalía media del Sol
    mp = 134.9633964 + 477198.8675055 * t  # anomalía media de la Luna
    f = 93.2720950 + 483202.0175233 * t  # argumento de latitud

    r = math.radians
    lon = (
        lp
        + 6.288774 * math.sin(r(mp))
        + 1.274027 * math.sin(r(2 * d - mp))
        + 0.658314 * math.sin(r(2 * d))
        + 0.213618 * math.sin(r(2 * mp))
        - 0.185116 * math.sin(r(m))
        - 0.114332 * math.sin(r(2 * f))
        + 0.058793 * math.sin(r(2 * d - 2 * mp))
        + 0.057066 * math.sin(r(2 * d - m - mp))
        + 0.053322 * math.sin(r(2 * d + mp))
        + 0.045758 * math.sin(r(2 * d - m))
    ) % 360.0
    lat = (
        5.128122 * math.sin(r(f))
        + 0.280602 * math.sin(r(mp + f))
        + 0.277693 * math.sin(r(mp - f))
        + 0.173237 * math.sin(r(2 * d - f))
        + 0.055413 * math.sin(r(2 * d - mp + f))
        + 0.046271 * math.sin(r(2 * d - mp - f))
    )
    distance_km = (
        385000.56
        - 20905.355 * math.cos(r(mp))
        - 3699.111 * math.cos(r(2 * d - mp))
        - 2955.968 * math.cos(r(2 * d))
        - 569.925 * math.cos(r(2 * mp))
    )

    eps = math.radians(_obliquity_deg(t))
    lam, beta = math.radians(lon), math.radians(lat)
    ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps), math.cos(lam))
    dec = math.asin(
        max(
            -1.0,
            min(
                1.0,
                math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam),
            ),
        )
    )

    sun_lon, sun_r_au = _sun_apparent(t)
    cos_elong = math.cos(beta) * math.cos(lam - math.radians(sun_lon))
    elong = math.acos(max(-1.0, min(1.0, cos_elong)))

    moon_r_au = distance_km / _AU_KM
    phase = math.atan2(sun_r_au * math.sin(elong), moon_r_au - sun_r_au * math.cos(elong))
    illumination = (1.0 + math.cos(phase)) / 2.0

    return MoonState(
        ra_deg=math.degrees(ra) % 360.0,
        dec_deg=math.degrees(dec),
        ecliptic_lon_deg=lon,
        ecliptic_lat_deg=lat,
        distance_km=distance_km,
        illumination=illumination,
        phase_angle_deg=math.degrees(phase),
        elongation_deg=math.degrees(elong),
    )


def moon_illumination(when: datetime) -> float:
    """Fracción iluminada del disco lunar (0–1) en ``when``. Ver :func:`moon_state`."""
    return moon_state(when).illumination


def moon_separation_deg(ra_deg: float, dec_deg: float, when: datetime) -> float:
    """Separación angular entre un objetivo J2000 y la Luna (geocéntrica).

    Junto con ``moon_illumination`` es lo que determina el fondo de cielo añadido
    por la Luna, que pesa tanto como la clase Bortle del sitio
    (``docs/research/…`` §"Light pollution").
    """
    moon = moon_state(when)
    return angular_separation_deg(ra_deg, dec_deg, moon.ra_deg, moon.dec_deg)


# --------------------------------------------------------------------------- #
# Óptica
# --------------------------------------------------------------------------- #
def diffraction_limit_arcsec(aperture_mm: float, wavelength_nm: float = 550.0) -> float:
    """Límite de difracción (criterio de Rayleigh) ``θ = 1.22·λ/D``, en arcsec.

    Es el techo duro de resolución angular del producto: combinar tomas de
    observadores distintos **no** sintetiza una apertura (regla dura 1 de
    ``CLAUDE.md``), así que ninguna reconstrucción puede prometer detalle por
    debajo de este valor para la mejor óptica contribuyente.

    Ejemplo: 100 mm a 550 nm ⇒ 1.384 arcsec.
    """
    if aperture_mm <= 0.0:
        raise ValueError("aperture_mm debe ser > 0")
    if wavelength_nm <= 0.0:
        raise ValueError("wavelength_nm debe ser > 0")
    theta_rad = 1.22 * (wavelength_nm * 1e-9) / (aperture_mm * 1e-3)
    return theta_rad * ARCSEC_PER_RADIAN


def pixel_scale_arcsec(focal_length_mm: float, pixel_pitch_um: float) -> float:
    """Escala de placa en arcsec/píxel: ``206.265 · pitch_µm / focal_mm``."""
    if focal_length_mm <= 0.0:
        raise ValueError("focal_length_mm debe ser > 0")
    if pixel_pitch_um <= 0.0:
        raise ValueError("pixel_pitch_um debe ser > 0")
    return ARCSEC_PER_RADIAN * (pixel_pitch_um * 1e-6) / (focal_length_mm * 1e-3)


def sampling_ratio(focal_length_mm: float, pixel_pitch_um: float, aperture_mm: float) -> float:
    """``pixel_scale / diffraction_limit``. > 1 ⇒ el sensor submuestrea la óptica.

    Este cociente es exactamente el margen que la reconstrucción multi-frame puede
    reclamar: un 50 mm con píxeles de 4 µm da ~16 arcsec/píxel frente a un límite
    óptico de ~2.8 arcsec, es decir un factor ~6 de detalle aliaseado y recuperable
    con dither sub-píxel (``docs/research/…`` §5). Por debajo de 1 (sobremuestreo)
    no hay nada que recuperar por esa vía, solo SNR.
    """
    return pixel_scale_arcsec(focal_length_mm, pixel_pitch_um) / diffraction_limit_arcsec(
        aperture_mm
    )
