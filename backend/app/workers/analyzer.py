"""Interfaz de análisis de imagen y sus dos implementaciones ligeras.

El análisis real (plate solving, medida de PSF, embeddings) vive en ``models/`` y
corre en AWS Batch con GPU. El backend **no** importa torch ni astropy: habla con el
análisis a través de :class:`ImageAnalyzer` y en el worker de ingesta usa una
implementación por defecto que solo hace lo barato (dimensiones, EXIF, previews).

Las dos implementaciones que se incluyen aquí:

* :class:`PillowImageAnalyzer` — lo que se puede hacer con Pillow y numpy: tamaño,
  profundidad de bits, EXIF/GPS, thumbnail, preview y una estimación grosera de
  fondo y SNR. No hace astrometría; deja la foto lista para que el job de plate
  solving la complete.
* :class:`StubImageAnalyzer` — determinista, sin IO, para los tests.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import structlog

__all__ = [
    "AnalysisResult",
    "ImageAnalyzer",
    "PillowImageAnalyzer",
    "StubImageAnalyzer",
]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class AnalysisResult:
    """Todo lo que el análisis puede aportar. Los campos desconocidos van a ``None``."""

    width_px: int | None = None
    height_px: int | None = None
    bit_depth: int | None = None
    mime_type: str | None = None
    captured_at_utc: datetime | None = None
    captured_at_local: datetime | None = None
    utc_offset_minutes: int | None = None
    lat: float | None = None
    lon: float | None = None
    elevation_m: float | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    lens_model: str | None = None
    focal_length_mm: float | None = None
    focal_ratio: float | None = None
    exposure_seconds: float | None = None
    iso: int | None = None
    background_adu: float | None = None
    snr_estimate: float | None = None
    star_count: int | None = None
    fwhm_arcsec: float | None = None
    eccentricity: float | None = None
    embedding: list[float] | None = None
    exif_raw: dict[str, Any] = field(default_factory=dict)
    #: Bytes del preview JPEG 2048 px y del thumb WebP 512 px.
    preview_bytes: bytes | None = None
    thumb_bytes: bytes | None = None


@runtime_checkable
class ImageAnalyzer(Protocol):
    """Contrato entre el worker de ingesta y el análisis de imagen."""

    async def analyze(self, data: bytes, *, filename: str) -> AnalysisResult:
        """Analiza los bytes de una imagen y devuelve lo que haya podido medir."""
        ...


# --------------------------------------------------------------------------- #
def _rational(value: Any) -> float | None:
    """Convierte los racionales de EXIF a float sin explotar con divisores 0."""
    try:
        if isinstance(value, tuple) and len(value) == 2:
            num, den = value
            return float(num) / float(den) if den else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_to_degrees(coord: Any, ref: Any) -> float | None:
    """``((d,1),(m,1),(s,100))`` + ``'N'`` → grados decimales con signo."""
    try:
        degrees, minutes, seconds = (float(_rational(v) or 0.0) for v in coord)
    except (TypeError, ValueError):
        return None
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if isinstance(ref, bytes):
        ref = ref.decode(errors="ignore")
    if str(ref).upper() in ("S", "W"):
        value = -value
    return value


class PillowImageAnalyzer:
    """Análisis barato con Pillow. Es la implementación por defecto del worker."""

    #: Lado mayor del preview JPEG de galería.
    PREVIEW_PX = 2048
    #: Lado mayor del thumbnail WebP.
    THUMB_PX = 512

    async def analyze(self, data: bytes, *, filename: str) -> AnalysisResult:
        from PIL import Image, ImageOps
        from PIL.ExifTags import GPSTAGS, TAGS

        result = AnalysisResult()
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as exc:
            log.warning("image_open_failed", filename=filename, error=type(exc).__name__)
            return result

        result.width_px, result.height_px = image.size
        result.mime_type = Image.MIME.get(image.format or "", None)
        result.bit_depth = {"1": 1, "L": 8, "RGB": 8, "RGBA": 8, "I;16": 16, "I": 32, "F": 32}.get(
            image.mode
        )

        exif_raw: dict[str, Any] = {}
        try:
            exif = image.getexif()
        except Exception:
            exif = None
        if exif:
            for tag_id, value in exif.items():
                name = TAGS.get(tag_id, str(tag_id))
                exif_raw[name] = value if isinstance(value, int | float | str) else str(value)

            result.camera_make = exif_raw.get("Make")
            result.camera_model = exif_raw.get("Model")
            result.lens_model = exif_raw.get("LensModel")
            result.focal_length_mm = _rational(exif.get(37386))
            result.focal_ratio = _rational(exif.get(33437))
            result.exposure_seconds = _rational(exif.get(33434))
            iso = exif.get(34855)
            result.iso = int(iso) if isinstance(iso, int) else None

            raw_dt = exif_raw.get("DateTimeOriginal") or exif_raw.get("DateTime")
            if isinstance(raw_dt, str):
                try:
                    result.captured_at_local = datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    log.info("unparsable_exif_datetime", value=raw_dt)

            try:
                gps = exif.get_ifd(0x8825)
            except Exception:
                gps = {}
            if gps:
                named = {GPSTAGS.get(k, str(k)): v for k, v in gps.items()}
                exif_raw["GPS"] = {k: str(v) for k, v in named.items()}
                result.lat = _gps_to_degrees(named.get("GPSLatitude"), named.get("GPSLatitudeRef"))
                result.lon = _gps_to_degrees(
                    named.get("GPSLongitude"), named.get("GPSLongitudeRef")
                )
                altitude = _rational(named.get("GPSAltitude"))
                if altitude is not None:
                    below = str(named.get("GPSAltitudeRef", 0)) in ("1", "b'\\x01'")
                    result.elevation_m = -altitude if below else altitude
        result.exif_raw = exif_raw

        # Estadística de fondo y SNR muy grosera: mediana como fondo y desviación
        # robusta (MAD × 1.4826) como ruido. No sustituye al worker de QA, pero da
        # un número comparable con el que ordenar hasta que ese worker pase.
        try:
            import numpy as np

            grey = ImageOps.grayscale(image)
            array = np.asarray(grey, dtype=np.float64)
            if array.size:
                background = float(np.median(array))
                mad = float(np.median(np.abs(array - background)))
                noise = mad * 1.4826
                result.background_adu = background
                if noise > 0:
                    peak = float(np.percentile(array, 99.9))
                    result.snr_estimate = max(0.0, (peak - background) / noise)
                # Proxy de número de estrellas: píxeles a más de 5σ del fondo.
                if noise > 0:
                    bright = int(np.count_nonzero(array > background + 5.0 * noise))
                    result.star_count = bright
        except Exception as exc:
            log.info("stats_failed", error=type(exc).__name__)

        result.preview_bytes = self._encode(image, self.PREVIEW_PX, "JPEG", quality=88)
        result.thumb_bytes = self._encode(image, self.THUMB_PX, "WEBP", quality=80)
        return result

    @staticmethod
    def _encode(image: Any, max_side: int, fmt: str, *, quality: int) -> bytes | None:
        try:
            copy = image.copy()
            copy.thumbnail((max_side, max_side))
            if copy.mode not in ("RGB", "L"):
                copy = copy.convert("RGB")
            buffer = io.BytesIO()
            copy.save(buffer, format=fmt, quality=quality)
            return buffer.getvalue()
        except Exception as exc:
            log.info("derivative_encode_failed", format=fmt, error=type(exc).__name__)
            return None


class StubImageAnalyzer:
    """Analizador determinista para tests: no abre la imagen, deriva de los bytes.

    Los valores salen de un hash de los datos, así que son estables entre
    ejecuciones (regla dura 3: reproducibilidad bit a bit) y distintos por fichero.
    """

    def __init__(self, *, with_gps: bool = True) -> None:
        self.with_gps = with_gps

    async def analyze(self, data: bytes, *, filename: str) -> AnalysisResult:
        import hashlib

        digest = hashlib.sha256(data + filename.encode()).digest()

        def unit(index: int) -> float:
            return digest[index] / 255.0

        return AnalysisResult(
            width_px=4000,
            height_px=3000,
            bit_depth=16,
            mime_type="image/tiff",
            focal_length_mm=200.0,
            focal_ratio=2.8,
            exposure_seconds=120.0,
            iso=1600,
            lat=(unit(0) * 180.0 - 90.0) if self.with_gps else None,
            lon=(unit(1) * 360.0 - 180.0) if self.with_gps else None,
            elevation_m=unit(2) * 3000.0 if self.with_gps else None,
            background_adu=100.0 + unit(3) * 900.0,
            snr_estimate=5.0 + unit(4) * 95.0,
            star_count=int(50 + unit(5) * 1950),
            fwhm_arcsec=1.5 + unit(6) * 5.0,
            eccentricity=unit(7) * 0.5,
            embedding=[math.sin(i + digest[i % 32]) for i in range(768)],
            exif_raw={"stub": True},
            preview_bytes=b"preview",
            thumb_bytes=b"thumb",
        )
