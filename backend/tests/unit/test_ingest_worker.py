"""Worker de ingesta: lo que se puede probar sin S3 ni base de datos.

``apply_analysis`` y ``compute_derived_metrics`` son funciones sueltas justamente
para esto: la parte de IO del worker se prueba en integración, y la lógica —que es
donde están los bugs— aquí.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.licensing import LicenseCode
from app.models.enums import LocationSource, PhotoStatus, TimeSource
from app.models.photo import Photo
from app.workers.analyzer import AnalysisResult, ImageAnalyzer, StubImageAnalyzer
from app.workers.ingest import DEFAULT_BORTLE, apply_analysis, compute_derived_metrics


def blank_photo(**kw: object) -> Photo:
    photo = Photo(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        status=PhotoStatus.PROCESSING,
        s3_bucket="b",
        s3_key_original="k",
        checksum_sha256=b"\x00" * 32,
        license=LicenseCode.CC_BY_NC,
        **kw,  # type: ignore[arg-type]
    )
    photo.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    photo.updated_at = photo.created_at
    return photo


# --------------------------------------------------------------------------- #
# El analizador stub
# --------------------------------------------------------------------------- #
def test_stub_analyzer_satisfies_the_protocol() -> None:
    assert isinstance(StubImageAnalyzer(), ImageAnalyzer)


async def test_stub_analyzer_is_deterministic() -> None:
    """Regla dura 3: misma entrada ⇒ misma salida."""
    analyzer = StubImageAnalyzer()
    first = await analyzer.analyze(b"bytes", filename="a.tif")
    second = await analyzer.analyze(b"bytes", filename="a.tif")
    assert first == second


async def test_stub_analyzer_varies_with_the_input() -> None:
    analyzer = StubImageAnalyzer()
    a = await analyzer.analyze(b"one", filename="a.tif")
    b = await analyzer.analyze(b"two", filename="a.tif")
    assert (a.lat, a.snr_estimate) != (b.lat, b.snr_estimate)


async def test_stub_analyzer_can_omit_gps() -> None:
    result = await StubImageAnalyzer(with_gps=False).analyze(b"x", filename="a.tif")
    assert result.lat is None and result.lon is None


async def test_stub_analyzer_returns_a_768_dim_embedding() -> None:
    result = await StubImageAnalyzer().analyze(b"x", filename="a.tif")
    assert result.embedding is not None
    assert len(result.embedding) == 768


# --------------------------------------------------------------------------- #
# apply_analysis: lo declarado por el usuario gana al EXIF
# --------------------------------------------------------------------------- #
def test_analysis_fills_only_the_empty_fields() -> None:
    photo = blank_photo(focal_length_mm=200.0)
    apply_analysis(
        photo,
        AnalysisResult(focal_length_mm=50.0, iso=800, width_px=6000, height_px=4000),
    )
    assert photo.focal_length_mm == 200.0  # el usuario gana
    assert photo.iso == 800  # el EXIF rellena el hueco
    assert photo.width_px == 6000


def test_user_declared_time_is_not_overwritten_by_exif() -> None:
    photo = blank_photo(
        captured_at_local=datetime(2026, 5, 1, 22, 0),
        utc_offset_minutes=120,
        time_source=TimeSource.USER,
    )
    apply_analysis(photo, AnalysisResult(captured_at_local=datetime(2020, 1, 1)))
    assert photo.time_source is TimeSource.USER
    assert photo.captured_at_local.year == 2026  # type: ignore[union-attr]


def test_exif_time_with_a_known_offset_becomes_utc() -> None:
    photo = blank_photo(utc_offset_minutes=120)
    apply_analysis(
        photo,
        AnalysisResult(captured_at_local=datetime(2026, 5, 1, 22, 0)),
    )
    assert photo.time_source is TimeSource.EXIF
    assert photo.captured_at_utc == datetime(2026, 5, 1, 20, 0, tzinfo=UTC)


def test_exif_time_without_offset_is_marked_inferred() -> None:
    """Sin offset la hora de pared no basta; se dice que es inferida, no se finge."""
    photo = blank_photo()
    apply_analysis(
        photo,
        AnalysisResult(captured_at_local=datetime(2026, 5, 1, 22, 0)),
    )
    assert photo.time_source is TimeSource.INFERRED
    assert photo.utc_offset_minutes == 0


def test_exif_gps_is_used_only_when_the_user_did_not_pin_a_location() -> None:
    photo = blank_photo()
    apply_analysis(photo, AnalysisResult(lat=28.3, lon=-16.5, elevation_m=2390.0))
    assert photo.location_source is LocationSource.EXIF_GPS
    assert photo.lat_deg == pytest.approx(28.3)  # type: ignore[attr-defined]
    assert photo.elevation_m == pytest.approx(2390.0)


def test_a_user_pin_wins_over_exif_gps() -> None:
    photo = blank_photo(location="SRID=4326;POINT(0 0)", location_source=LocationSource.USER_PIN)
    apply_analysis(photo, AnalysisResult(lat=28.3, lon=-16.5))
    assert photo.location_source is LocationSource.USER_PIN


def test_aperture_and_pixel_pitch_are_derived() -> None:
    photo = blank_photo(focal_ratio=2.8, sensor_width_mm=36.0)
    apply_analysis(photo, AnalysisResult(focal_length_mm=200.0, width_px=6000))
    assert photo.aperture_mm == pytest.approx(200.0 / 2.8)
    # 36 mm sobre 6000 px = 6 µm por píxel.
    assert photo.pixel_pitch_um == pytest.approx(6.0)


# --------------------------------------------------------------------------- #
# compute_derived_metrics
# --------------------------------------------------------------------------- #
def test_derived_metrics_need_no_database() -> None:
    photo = blank_photo(
        focal_length_mm=530.0,
        focal_ratio=5.0,
        pixel_pitch_um=3.76,
        ra_deg=10.6847,
        dec_deg=41.269,
        captured_at_utc=datetime(2026, 10, 15, 23, 0, tzinfo=UTC),
        fwhm_arcsec=2.4,
        snr_estimate=45.0,
        star_count=900,
        eccentricity=0.15,
    )
    photo.lat_deg = 40.4  # type: ignore[attr-defined]
    photo.lon_deg = -3.7  # type: ignore[attr-defined]

    metrics = compute_derived_metrics(photo)
    assert metrics.aperture_mm == pytest.approx(106.0)
    assert metrics.diffraction_limit_arcsec == pytest.approx(1.30, abs=0.02)
    assert metrics.pixel_scale_arcsec == pytest.approx(1.463, abs=0.005)
    assert metrics.airmass is not None and metrics.airmass >= 1.0
    assert metrics.moon_illumination is not None
    assert 0.0 <= metrics.moon_illumination <= 1.0
    assert metrics.moon_separation_deg is not None
    assert 0.0 <= metrics.moon_separation_deg <= 180.0
    assert metrics.quality_score is not None
    assert 0.0 < metrics.quality_score < 1.0


def test_derived_metrics_are_deterministic() -> None:
    photo = blank_photo(
        focal_length_mm=200.0,
        focal_ratio=2.8,
        pixel_pitch_um=4.0,
        ra_deg=83.8,
        dec_deg=-5.4,
        captured_at_utc=datetime(2026, 1, 20, 2, 0, tzinfo=UTC),
        fwhm_arcsec=3.0,
    )
    photo.lat_deg = 28.3  # type: ignore[attr-defined]
    photo.lon_deg = -16.5  # type: ignore[attr-defined]
    assert compute_derived_metrics(photo) == compute_derived_metrics(photo)


def test_an_object_below_the_horizon_reports_no_airmass() -> None:
    """``inf`` no cabe en un ``real`` de Postgres: se guarda NULL."""
    photo = blank_photo(
        ra_deg=0.0,
        dec_deg=-89.0,
        captured_at_utc=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )
    photo.lat_deg = 60.0  # type: ignore[attr-defined]
    photo.lon_deg = 10.0  # type: ignore[attr-defined]
    metrics = compute_derived_metrics(photo)
    assert metrics.airmass is None


def test_a_photo_without_metadata_still_gets_a_score() -> None:
    """Nada conocido ⇒ puntuación neutra, no un crash ni un cero injusto."""
    metrics = compute_derived_metrics(blank_photo())
    assert metrics.quality_score is not None
    assert 0.0 <= metrics.quality_score <= 1.0
    assert metrics.airmass is None
    assert metrics.pixel_scale_arcsec is None


def test_missing_bortle_falls_back_to_the_documented_default() -> None:
    from app.domain.quality import quality_score

    photo = blank_photo(fwhm_arcsec=2.0)
    expected = quality_score(fwhm_arcsec=2.0, bortle_estimate=DEFAULT_BORTLE).score
    assert compute_derived_metrics(photo).quality_score == pytest.approx(expected)
