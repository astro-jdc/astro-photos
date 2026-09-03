"""Consumidor SQS de ingesta.

Flujo por mensaje:

1. Descarga el original de S3 (a memoria; el binario nunca toca la API HTTP).
2. Extrae EXIF/XMP, incluida la GPS, con el :class:`ImageAnalyzer` inyectado.
3. Genera preview (JPEG 2048) y thumb (WebP 512) y los sube a ``derived``.
4. Encola el plate solving.
5. Calcula las métricas derivadas que **sí** son puras (alt/az, masa de aire, Luna,
   escala de placa, límite de difracción, ``quality_score``) con ``app.domain``.
6. Marca ``status=ready``.

Los campos que el usuario declaró en ``complete`` **ganan** al EXIF: aquí solo se
rellena lo que esté a ``None`` (``docs/api.md``).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import Settings, get_settings
from app.domain.astro import (
    airmass,
    alt_az,
    diffraction_limit_arcsec,
    moon_separation_deg,
    moon_state,
    pixel_scale_arcsec,
)
from app.domain.quality import quality_score
from app.models.enums import LocationSource, PhotoStatus, TimeSource
from app.models.photo import Photo
from app.services.queue import QueueService
from app.services.storage import StorageService
from app.workers.analyzer import AnalysisResult, ImageAnalyzer, PillowImageAnalyzer

__all__ = ["IngestWorker", "apply_analysis", "compute_derived_metrics"]

log = structlog.get_logger(__name__)

#: Bortle asumido cuando no hay mapa de contaminación lumínica. 5 es la mediana de
#: un cielo suburbano europeo; se marca como estimado y el worker de QA lo refina.
DEFAULT_BORTLE = 5


@dataclass(slots=True)
class DerivedMetrics:
    """Métricas que se calculan sin mirar los píxeles, solo con la metadata."""

    airmass: float | None = None
    altitude_deg: float | None = None
    azimuth_deg: float | None = None
    moon_illumination: float | None = None
    moon_separation_deg: float | None = None
    pixel_scale_arcsec: float | None = None
    aperture_mm: float | None = None
    diffraction_limit_arcsec: float | None = None
    quality_score: float | None = None


def apply_analysis(photo: Photo, analysis: AnalysisResult) -> None:
    """Vuelca el análisis sobre la foto **sin pisar lo que declaró el usuario**."""
    for field_name in (
        "width_px",
        "height_px",
        "bit_depth",
        "camera_make",
        "camera_model",
        "lens_model",
        "focal_length_mm",
        "focal_ratio",
        "exposure_seconds",
        "iso",
        "background_adu",
        "snr_estimate",
        "star_count",
        "fwhm_arcsec",
        "eccentricity",
    ):
        value = getattr(analysis, field_name)
        if value is not None and getattr(photo, field_name) is None:
            setattr(photo, field_name, value)

    if analysis.mime_type and not photo.mime_type:
        photo.mime_type = analysis.mime_type
    if analysis.exif_raw:
        photo.exif_raw = analysis.exif_raw
    if analysis.embedding is not None and photo.embedding is None:
        photo.embedding = analysis.embedding

    # Tiempo: solo si el usuario no lo fijó (time_source != 'user').
    if photo.time_source is not TimeSource.USER and analysis.captured_at_local is not None:
        photo.captured_at_local = analysis.captured_at_local
        photo.time_source = TimeSource.EXIF
        if photo.utc_offset_minutes is not None:
            photo.captured_at_utc = analysis.captured_at_local.replace(tzinfo=UTC) - timedelta(
                minutes=photo.utc_offset_minutes
            )
        elif photo.captured_at_utc is None:
            # Sin offset la hora de pared no basta; se marca como inferida y se
            # asume UTC, que es lo único honesto que se puede hacer.
            photo.captured_at_utc = analysis.captured_at_local.replace(tzinfo=UTC)
            photo.utc_offset_minutes = 0
            photo.time_source = TimeSource.INFERRED

    # Lugar: la GPS del EXIF solo si el usuario no puso un pin.
    if photo.location is None and analysis.lat is not None and analysis.lon is not None:
        photo.location = f"SRID=4326;POINT({analysis.lon} {analysis.lat})"
        photo.location_source = LocationSource.EXIF_GPS
        photo.lat_deg = analysis.lat  # type: ignore[attr-defined]  # transitorio
        photo.lon_deg = analysis.lon  # type: ignore[attr-defined]
        if photo.elevation_m is None:
            photo.elevation_m = analysis.elevation_m

    if photo.focal_length_mm and photo.focal_ratio and photo.aperture_mm is None:
        photo.aperture_mm = photo.focal_length_mm / photo.focal_ratio
    if photo.pixel_pitch_um is None and photo.sensor_width_mm and photo.width_px:
        photo.pixel_pitch_um = (photo.sensor_width_mm * 1000.0) / photo.width_px


def compute_derived_metrics(photo: Photo) -> DerivedMetrics:
    """Métricas puras a partir de la metadata. Todo sale de ``app.domain``.

    Es una función aparte (y sin IO) justamente para poder testearla sin base de
    datos y para que el recálculo masivo pueda reutilizarla.
    """
    metrics = DerivedMetrics()

    if photo.focal_length_mm and photo.focal_ratio:
        metrics.aperture_mm = photo.focal_length_mm / photo.focal_ratio
        metrics.diffraction_limit_arcsec = diffraction_limit_arcsec(metrics.aperture_mm)
    if photo.focal_length_mm and photo.pixel_pitch_um:
        metrics.pixel_scale_arcsec = pixel_scale_arcsec(photo.focal_length_mm, photo.pixel_pitch_um)

    lat = getattr(photo, "lat_deg", None)
    lon = getattr(photo, "lon_deg", None)
    when = photo.captured_at_utc

    if when is not None:
        moon = moon_state(when)
        metrics.moon_illumination = moon.illumination
        if photo.ra_deg is not None and photo.dec_deg is not None:
            metrics.moon_separation_deg = moon_separation_deg(photo.ra_deg, photo.dec_deg, when)

    if (
        when is not None
        and lat is not None
        and lon is not None
        and photo.ra_deg is not None
        and photo.dec_deg is not None
    ):
        horizontal = alt_az(photo.ra_deg, photo.dec_deg, float(lat), float(lon), when)
        metrics.altitude_deg = horizontal.altitude_deg
        metrics.azimuth_deg = horizontal.azimuth_deg
        value = airmass(horizontal.altitude_deg)
        metrics.airmass = value if value != float("inf") else None

    breakdown = quality_score(
        fwhm_arcsec=photo.fwhm_arcsec,
        eccentricity=photo.eccentricity,
        snr_estimate=photo.snr_estimate,
        star_count=photo.star_count,
        airmass=metrics.airmass if metrics.airmass is not None else photo.airmass,
        moon_illumination=(
            metrics.moon_illumination
            if metrics.moon_illumination is not None
            else photo.moon_illumination
        ),
        moon_separation_deg=(
            metrics.moon_separation_deg
            if metrics.moon_separation_deg is not None
            else photo.moon_separation_deg
        ),
        bortle_estimate=photo.bortle_estimate or DEFAULT_BORTLE,
    )
    metrics.quality_score = breakdown.score
    return metrics


class IngestWorker:
    """Consumidor de la cola de ingesta. Se arranca con ``python -m app.workers.ingest``."""

    def __init__(
        self,
        *,
        storage: StorageService,
        queue: QueueService,
        analyzer: ImageAnalyzer | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.storage = storage
        self.queue = queue
        self.analyzer: ImageAnalyzer = analyzer or PillowImageAnalyzer()
        self.settings = settings or get_settings()
        self._running = False

    # ------------------------------------------------------------------ #
    async def process(self, body: dict[str, Any]) -> None:
        """Procesa un mensaje. Importa la sesión aquí para no atarla al constructor."""
        from sqlalchemy import select

        from app.db.session import session_scope

        photo_id = body.get("photo_id")
        if not photo_id:
            log.warning("ingest_message_without_photo_id")
            return

        async with session_scope() as session:
            photo = (
                await session.execute(select(Photo).where(Photo.id == uuid.UUID(photo_id)))
            ).scalar_one_or_none()
            if photo is None:
                log.warning("ingest_photo_missing", photo_id=photo_id)
                return
            if photo.status is PhotoStatus.READY:
                # Idempotencia: un reintento de SQS no debe reprocesar ni duplicar.
                log.info("ingest_already_done", photo_id=photo_id)
                return

            try:
                data = await self._download(photo)
                analysis = await self.analyzer.analyze(
                    data, filename=photo.s3_key_original.rsplit("/", 1)[-1]
                )
                apply_analysis(photo, analysis)
                await self._store_derivatives(photo, analysis)

                metrics = compute_derived_metrics(photo)
                photo.airmass = metrics.airmass
                photo.moon_illumination = metrics.moon_illumination
                photo.moon_separation_deg = metrics.moon_separation_deg
                photo.quality_score = metrics.quality_score
                if photo.aperture_mm is None:
                    photo.aperture_mm = metrics.aperture_mm
                if photo.pixel_scale_arcsec is None:
                    photo.pixel_scale_arcsec = metrics.pixel_scale_arcsec
                if photo.bortle_estimate is None:
                    photo.bortle_estimate = DEFAULT_BORTLE

                await self._enqueue_plate_solving(photo)
                photo.status = PhotoStatus.READY
                log.info(
                    "ingest_done",
                    photo_id=photo_id,
                    quality_score=photo.quality_score,
                )
            except Exception as exc:
                photo.status = PhotoStatus.FAILED
                log.exception("ingest_failed", photo_id=photo_id, error=type(exc).__name__)

    async def _download(self, photo: Photo) -> bytes:
        """Descarga el original de S3 a memoria, en un hilo (boto3 es bloqueante)."""
        client = self.storage._client
        buffer = await asyncio.to_thread(
            client.get_object, Bucket=photo.s3_bucket, Key=photo.s3_key_original
        )
        return bytes(await asyncio.to_thread(buffer["Body"].read))

    async def _store_derivatives(self, photo: Photo, analysis: AnalysisResult) -> None:
        client = self.storage._client
        bucket = self.settings.s3_bucket_derived
        if analysis.preview_bytes:
            key = self.storage.derived_key(str(photo.id), "preview", "jpg")
            await asyncio.to_thread(
                client.put_object,
                Bucket=bucket,
                Key=key,
                Body=analysis.preview_bytes,
                ContentType="image/jpeg",
            )
            photo.s3_key_preview = key
        if analysis.thumb_bytes:
            key = self.storage.derived_key(str(photo.id), "thumb", "webp")
            await asyncio.to_thread(
                client.put_object,
                Bucket=bucket,
                Key=key,
                Body=analysis.thumb_bytes,
                ContentType="image/webp",
            )
            photo.s3_key_thumb = key

    async def _enqueue_plate_solving(self, photo: Photo) -> None:
        """El plate solving corre fuera: astrometry.net o ASTAP en Batch."""
        await self.queue.send(
            self.settings.sqs_queue_reconstruct,
            {
                "type": "plate_solve",
                "photo_id": str(photo.id),
                "bucket": photo.s3_bucket,
                "key": photo.s3_key_original,
                "hint_ra": photo.ra_deg,
                "hint_dec": photo.dec_deg,
                "enqueued_at": datetime.now(UTC).isoformat(),
            },
            idempotency_key=f"solve:{photo.id}",
        )

    # ------------------------------------------------------------------ #
    async def run(self, *, max_iterations: int | None = None) -> None:
        """Bucle de long polling. ``max_iterations`` acota el bucle en tests."""
        self._running = True
        iterations = 0
        while self._running:
            messages = await self.queue.receive(self.settings.sqs_queue_ingest)
            for message in messages:
                await self.process(message.body)
                await self.queue.delete(self.settings.sqs_queue_ingest, message.receipt_handle)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return

    def stop(self) -> None:
        self._running = False


async def main() -> None:  # pragma: no cover - punto de entrada
    settings = get_settings()
    worker = IngestWorker(
        storage=StorageService(settings),
        queue=QueueService(settings),
        settings=settings,
    )
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
