"""Imágenes de prueba con verdad conocida.

Un campo estelar sintético: estrellas puntuales convolucionadas con una PSF
gaussiana, más fondo de cielo y ruido de Poisson. Como las posiciones y los flujos
los ponemos nosotros, cualquier test puede comprobar fotometría, FWHM o SNR contra
la verdad en vez de contra "lo que salió la vez anterior".

El EXIF lleva GPS de verdad (formato racional de EXIF, referencias N/S y E/W), que
es lo que necesita el test de privacidad: sin GPS en el fichero no se puede
demostrar que el backend lo borra.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
import piexif
from PIL import Image

__all__ = [
    "Star",
    "SyntheticImage",
    "TEIDE_LAT",
    "TEIDE_LON",
    "gps_ifd",
    "make_star_field",
    "read_gps",
]

#: Observatorio del Teide. Un punto reconocible: si aparece en una respuesta que
#: debería estar oculta, el fallo es evidente al leerlo.
TEIDE_LAT = 28.300224
TEIDE_LON = -16.512306
TEIDE_ELEVATION_M = 2390.0


@dataclass(frozen=True)
class Star:
    """Una fuente puntual de verdad conocida."""

    x: float
    y: float
    #: Flujo total en cuentas (ADU). La suma sobre la PSF debe conservarse.
    flux: float


@dataclass(frozen=True)
class SyntheticImage:
    """Imagen + la verdad con la que se generó."""

    data: np.ndarray
    stars: tuple[Star, ...]
    fwhm_px: float
    background: float

    @property
    def total_star_flux(self) -> float:
        return float(sum(s.flux for s in self.stars))


def _gaussian_psf(shape: tuple[int, int], star: Star, sigma: float) -> np.ndarray:
    """PSF gaussiana normalizada a flujo unidad sobre el recorte completo.

    Se normaliza por la suma real del array, no por 2*pi*sigma^2 analítico: con la
    imagen truncada en los bordes el factor analítico no conserva el flujo y los
    tests de fotometría fallarían por una razón que no es la que buscan.
    """
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    r2 = (xx - star.x) ** 2 + (yy - star.y) ** 2
    psf = np.exp(-r2 / (2.0 * sigma**2))
    total = psf.sum()
    return psf / total if total > 0 else psf


def make_star_field(
    *,
    width: int = 256,
    height: int = 256,
    stars: tuple[Star, ...] | None = None,
    fwhm_px: float = 3.5,
    background: float = 120.0,
    read_noise: float = 4.0,
    seed: int = 20260903,
    noise: bool = True,
) -> SyntheticImage:
    """Campo estelar reproducible.

    `seed` es obligatorio de facto: el generador se construye explícito y nunca se
    usa el `numpy.random` global, para que dos llamadas con la misma semilla den
    exactamente el mismo array (regla 3 de `CLAUDE.md`).
    """
    if stars is None:
        stars = (
            Star(x=64.0, y=64.0, flux=50_000.0),
            Star(x=128.3, y=96.7, flux=20_000.0),
            Star(x=190.0, y=170.0, flux=8_000.0),
            Star(x=40.5, y=200.2, flux=3_000.0),
        )
    sigma = fwhm_px / (2.0 * np.sqrt(2.0 * np.log(2.0)))

    frame = np.full((height, width), float(background), dtype=np.float64)
    for star in stars:
        frame += star.flux * _gaussian_psf((height, width), star, sigma)

    if noise:
        rng = np.random.default_rng(seed)
        frame = rng.poisson(np.clip(frame, 0, None)).astype(np.float64)
        frame += rng.normal(0.0, read_noise, size=frame.shape)

    return SyntheticImage(
        data=frame, stars=tuple(stars), fwhm_px=fwhm_px, background=float(background)
    )


def _to_rational(value: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Grados decimales -> (grados, minutos, segundos) racionales de EXIF."""
    value = abs(value)
    degrees = int(value)
    minutes_full = (value - degrees) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    sec = Fraction(seconds).limit_denominator(10_000)
    return ((degrees, 1), (minutes, 1), (sec.numerator, sec.denominator))


def gps_ifd(
    lat: float = TEIDE_LAT,
    lon: float = TEIDE_LON,
    elevation_m: float = TEIDE_ELEVATION_M,
) -> dict[int, object]:
    """Bloque GPS de EXIF completo, tal como lo escribe una cámara real."""
    alt = Fraction(abs(elevation_m)).limit_denominator(100)
    return {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _to_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _to_rational(lon),
        piexif.GPSIFD.GPSAltitudeRef: 0 if elevation_m >= 0 else 1,
        piexif.GPSIFD.GPSAltitude: (alt.numerator, alt.denominator),
    }


def encode_jpeg_with_gps(
    image: SyntheticImage,
    *,
    lat: float = TEIDE_LAT,
    lon: float = TEIDE_LON,
    elevation_m: float = TEIDE_ELEVATION_M,
    camera_make: str = "Canon",
    camera_model: str = "EOS Ra",
    quality: int = 92,
) -> bytes:
    """JPEG de 8 bits con EXIF **incluyendo GPS**.

    Devuelve los bytes exactos que se subirán a MinIO, para poder calcular su
    SHA-256 y comparar lo que S3 guardó con lo que se mandó.
    """
    scaled = image.data - image.data.min()
    peak = scaled.max()
    if peak > 0:
        scaled = scaled / peak
    pixels = (np.clip(scaled, 0.0, 1.0) * 255.0).astype(np.uint8)
    pil = Image.fromarray(pixels, mode="L").convert("RGB")

    exif = {
        "0th": {
            piexif.ImageIFD.Make: camera_make.encode(),
            piexif.ImageIFD.Model: camera_model.encode(),
            piexif.ImageIFD.Software: b"astro-photos qa synthetic",
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: b"2026:02:14 23:41:07",
            piexif.ExifIFD.ExposureTime: (120, 1),
            piexif.ExifIFD.ISOSpeedRatings: 800,
            piexif.ExifIFD.FNumber: (56, 10),
            piexif.ExifIFD.FocalLength: (600, 1),
        },
        "GPS": gps_ifd(lat, lon, elevation_m),
        "1st": {},
        "thumbnail": None,
    }

    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality, exif=piexif.dump(exif))
    return buf.getvalue()


def read_gps(payload: bytes) -> dict[str, object]:
    """Lee de vuelta el bloque GPS de unos bytes JPEG. `{}` si no hay.

    Es lo que usa el test de privacidad para afirmar que el fichero servido no
    lleva coordenadas: se lee el EXIF real, no un campo del JSON.
    """
    try:
        exif = piexif.load(payload)
    except Exception:  # noqa: BLE001 - un fichero sin EXIF válido no tiene GPS
        return {}
    return {str(k): v for k, v in (exif.get("GPS") or {}).items()}


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
