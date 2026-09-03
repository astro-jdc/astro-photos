"""Tests de ``app.domain.astro`` contra valores conocidos."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from app.domain.astro import (
    airmass,
    alt_az,
    angular_separation_deg,
    diffraction_limit_arcsec,
    julian_date,
    moon_illumination,
    moon_separation_deg,
    moon_state,
    pixel_scale_arcsec,
    sampling_ratio,
)

J2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Masa de aire
# --------------------------------------------------------------------------- #
def test_airmass_at_zenith_is_one() -> None:
    assert airmass(90.0) == pytest.approx(1.0, abs=1e-5)


def test_airmass_grows_monotonically_with_zenith_distance() -> None:
    altitudes = [90, 80, 70, 60, 45, 30, 20, 10, 5, 2, 1]
    values = [airmass(a) for a in altitudes]
    assert values == sorted(values)
    assert all(math.isfinite(v) for v in values)


@pytest.mark.parametrize(
    ("altitude", "expected"),
    [
        (60.0, 1.1547),  # sec(30°)
        (30.0, 2.0),  # sec(60°), la aproximación sigue siendo buena aquí
    ],
)
def test_airmass_matches_secant_where_the_secant_is_still_valid(
    altitude: float, expected: float
) -> None:
    assert airmass(altitude) == pytest.approx(expected, rel=0.01)


def test_airmass_diverges_from_the_naive_secant_near_the_horizon() -> None:
    """A 1° de altitud sec(z) da 57.3 y la atmósfera real ~27: la mitad de camino."""
    naive = 1.0 / math.sin(math.radians(1.0))
    assert naive == pytest.approx(57.3, abs=0.1)
    assert airmass(1.0) == pytest.approx(26.6, abs=0.5)
    assert airmass(1.0) < naive / 2.0


def test_airmass_below_the_horizon_is_infinite() -> None:
    assert airmass(0.0) == math.inf
    assert airmass(-10.0) == math.inf


def test_kasten_young_agrees_with_pickering_away_from_the_horizon() -> None:
    for altitude in (80.0, 60.0, 40.0, 20.0):
        assert airmass(altitude, "kasten-young") == pytest.approx(
            airmass(altitude, "pickering"), rel=0.01
        )


def test_unknown_airmass_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="Modelo de masa de aire"):
        airmass(45.0, "sec-z")  # type: ignore[arg-type]  # entrada inválida a propósito


# --------------------------------------------------------------------------- #
# Óptica
# --------------------------------------------------------------------------- #
def test_diffraction_limit_of_a_100mm_aperture() -> None:
    """1.22·λ/D con λ=550 nm y D=100 mm ⇒ 1.384 arcsec."""
    assert diffraction_limit_arcsec(100.0) == pytest.approx(1.38, abs=0.01)


def test_diffraction_limit_scales_inversely_with_aperture() -> None:
    assert diffraction_limit_arcsec(200.0) == pytest.approx(diffraction_limit_arcsec(100.0) / 2.0)


def test_diffraction_limit_scales_with_wavelength() -> None:
    blue = diffraction_limit_arcsec(100.0, 450.0)
    red = diffraction_limit_arcsec(100.0, 650.0)
    assert blue < red


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_diffraction_limit_rejects_nonsense_apertures(bad: float) -> None:
    with pytest.raises(ValueError, match="aperture_mm"):
        diffraction_limit_arcsec(bad)


def test_pixel_scale_of_a_typical_setup() -> None:
    """206.265·pitch/focal: 3.76 µm en 530 mm ⇒ 1.46 arcsec/px."""
    assert pixel_scale_arcsec(530.0, 3.76) == pytest.approx(1.463, abs=0.005)


def test_pixel_scale_of_a_wide_field_lens_shows_the_undersampling_gap() -> None:
    """Un 50 mm con píxeles de 4 µm da ~16.5 arcsec/px (``docs/research/…`` §5)."""
    assert pixel_scale_arcsec(50.0, 4.0) == pytest.approx(16.5, abs=0.1)


def test_sampling_ratio_flags_the_recoverable_aliasing() -> None:
    """50 mm f/1.8 (aperture ≈ 28 mm), píxeles de 4 µm: submuestreo de ~3x."""
    ratio = sampling_ratio(focal_length_mm=50.0, pixel_pitch_um=4.0, aperture_mm=27.8)
    assert ratio > 3.0


def test_sampling_ratio_below_one_means_oversampled() -> None:
    ratio = sampling_ratio(focal_length_mm=2350.0, pixel_pitch_um=3.76, aperture_mm=235.0)
    assert ratio < 1.0


# --------------------------------------------------------------------------- #
# Separaciones angulares
# --------------------------------------------------------------------------- #
def test_separation_of_a_point_with_itself_is_zero() -> None:
    assert angular_separation_deg(83.822, -5.391, 83.822, -5.391) == pytest.approx(0.0)


def test_separation_along_the_equator_equals_the_ra_difference() -> None:
    assert angular_separation_deg(0.0, 0.0, 10.0, 0.0) == pytest.approx(10.0)


def test_separation_between_the_poles_is_180_degrees() -> None:
    assert angular_separation_deg(0.0, 90.0, 0.0, -90.0) == pytest.approx(180.0)


def test_separation_wraps_around_ra_zero() -> None:
    assert angular_separation_deg(359.0, 0.0, 1.0, 0.0) == pytest.approx(2.0)


def test_separation_mizar_alcor() -> None:
    """Mizar y Alcor están separadas ~11.8 arcmin (0.196°); el par visual clásico."""
    mizar = (200.9814, 54.9254)
    alcor = (201.3062, 54.9878)
    separation = angular_separation_deg(*mizar, *alcor)
    assert separation == pytest.approx(0.196, abs=0.01)


def test_separation_is_symmetric() -> None:
    a, b = (10.0, 20.0), (200.0, -45.0)
    assert angular_separation_deg(*a, *b) == pytest.approx(angular_separation_deg(*b, *a))


def test_separation_is_numerically_stable_for_tiny_angles() -> None:
    """acos() perdería todos los dígitos aquí; Vincenty no."""
    tiny = angular_separation_deg(0.0, 0.0, 1e-7, 0.0)
    assert tiny == pytest.approx(1e-7, rel=1e-3)


# --------------------------------------------------------------------------- #
# Tiempo y coordenadas horizontales
# --------------------------------------------------------------------------- #
def test_julian_date_of_j2000_epoch() -> None:
    assert julian_date(J2000) == pytest.approx(2451545.0, abs=1e-6)


def test_julian_date_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        julian_date(datetime(2026, 1, 1, 0, 0, 0))


def test_object_at_the_observer_zenith_has_altitude_ninety() -> None:
    """Un objeto cuya Dec iguala la latitud culmina en el cenit al pasar el meridiano.

    Se busca el instante de tránsito ajustando la RA al tiempo sidéreo local.
    """
    when = datetime(2026, 6, 21, 0, 0, 0, tzinfo=UTC)
    lat, lon = 28.3, -16.5
    # RA = LST en el tránsito. Se obtiene invirtiendo alt_az con una búsqueda directa.
    best = max(
        (alt_az(ra / 10.0, lat, lat, lon, when).altitude_deg, ra / 10.0) for ra in range(0, 3600)
    )
    altitude, _ = best
    assert altitude == pytest.approx(90.0, abs=0.1)


def test_circumpolar_object_never_sets_from_a_high_latitude() -> None:
    """Polaris desde Tromsø (69.6° N) está siempre a ~69° de altitud."""
    polaris = (37.9545, 89.2641)
    for hour in range(0, 24, 3):
        when = datetime(2026, 3, 15, hour, tzinfo=UTC)
        horizontal = alt_az(*polaris, 69.65, 18.96, when)
        assert horizontal.altitude_deg == pytest.approx(69.65, abs=1.5)


def test_altitude_of_the_south_celestial_pole_is_negative_from_the_north() -> None:
    horizontal = alt_az(0.0, -90.0, 40.0, 0.0, J2000)
    assert horizontal.altitude_deg == pytest.approx(-40.0, abs=0.1)


def test_azimuth_is_reported_from_north_eastwards() -> None:
    """Un objeto en el polo norte celeste se ve exactamente al norte (az=0)."""
    horizontal = alt_az(0.0, 90.0, 40.0, 0.0, J2000)
    assert horizontal.azimuth_deg == pytest.approx(0.0, abs=0.5) or (
        horizontal.azimuth_deg == pytest.approx(360.0, abs=0.5)
    )
    assert horizontal.altitude_deg == pytest.approx(40.0, abs=0.1)


def test_altitude_never_leaves_its_range() -> None:
    for hour in range(24):
        when = datetime(2026, 8, 1, hour, tzinfo=UTC)
        horizontal = alt_az(120.0, 30.0, 45.0, 5.0, when)
        assert -90.0 <= horizontal.altitude_deg <= 90.0
        assert 0.0 <= horizontal.azimuth_deg < 360.0


# --------------------------------------------------------------------------- #
# Luna
# --------------------------------------------------------------------------- #
def test_moon_illumination_stays_in_range_over_two_years() -> None:
    for day in range(0, 730, 7):
        when = datetime(2026, 1, 1, tzinfo=UTC).replace(
            month=1 + (day // 30) % 12, day=1 + day % 28
        )
        assert 0.0 <= moon_illumination(when) <= 1.0


def test_moon_illumination_cycles_with_the_synodic_month() -> None:
    """Medio ciclo sinódico (~14.77 días) invierte la fase."""
    import datetime as dt

    start = datetime(2026, 3, 3, 12, tzinfo=UTC)  # cerca de luna llena
    full = moon_illumination(start)
    new = moon_illumination(start + dt.timedelta(days=14.77))
    assert abs(full - new) > 0.6


def test_moon_illumination_near_a_known_full_moon() -> None:
    """Luna llena del 3 de marzo de 2026 (~11:38 UTC): iluminación ~1."""
    assert moon_illumination(datetime(2026, 3, 3, 11, 38, tzinfo=UTC)) > 0.97


def test_moon_illumination_near_a_known_new_moon() -> None:
    """Luna nueva del 17 de marzo de 2026 (~13:23 UTC): iluminación ~0."""
    assert moon_illumination(datetime(2026, 3, 17, 13, 23, tzinfo=UTC)) < 0.03


def test_moon_state_is_self_consistent() -> None:
    state = moon_state(datetime(2026, 7, 4, 3, tzinfo=UTC))
    assert 0.0 <= state.ra_deg < 360.0
    assert -30.0 < state.dec_deg < 30.0  # la Luna nunca sale de ±28.6° de Dec
    assert 356_000 < state.distance_km < 407_000  # perigeo y apogeo reales
    assert abs(state.ecliptic_lat_deg) < 6.0  # inclinación orbital de 5.14°
    assert 0.0 <= state.elongation_deg <= 180.0


def test_moon_separation_is_a_valid_angle() -> None:
    when = datetime(2026, 9, 3, 22, tzinfo=UTC)
    separation = moon_separation_deg(10.68, 41.27, when)  # M31
    assert 0.0 <= separation <= 180.0


def test_moon_separation_from_the_moon_itself_is_zero() -> None:
    when = datetime(2026, 5, 12, 1, tzinfo=UTC)
    state = moon_state(when)
    assert moon_separation_deg(state.ra_deg, state.dec_deg, when) == pytest.approx(0.0, abs=1e-6)
