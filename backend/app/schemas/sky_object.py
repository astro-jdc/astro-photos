"""Schemas de ``/objects`` y del mapa de cobertura."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.models.enums import ObjectCatalog, ObjectType
from app.schemas.common import Schema

__all__ = ["CoverageCell", "CoverageOut", "ObjectOut"]


class ObjectOut(Schema):
    id: UUID
    catalog: ObjectCatalog
    catalog_number: str
    designation: str
    common_name: str | None = None
    common_name_es: str | None = None
    object_type: ObjectType
    ra_deg: float | None = None
    dec_deg: float | None = None
    magnitude: float | None = None
    size_arcmin: float | None = None
    aliases: list[str] = Field(default_factory=list)
    is_ephemeral: bool = False
    photo_count: int = 0
    reconstruction_count: int = 0


class CoverageCell(Schema):
    """Una celda del histograma tiempo × latitud × focal."""

    #: Mes en formato ``YYYY-MM`` (el eje temporal del widget).
    month: str
    #: Banda de latitud del observador, en grados: el límite inferior de la banda.
    lat_band_deg: int
    #: Banda de focal, en mm: el límite inferior de la banda (escala logarítmica).
    focal_band_mm: int
    photo_count: int
    #: Media de ``quality_score`` en la celda; ``None`` si ninguna foto la tiene.
    mean_quality: float | None = None


class CoverageGap(Schema):
    """Un hueco detectado, en texto listo para el widget."""

    kind: str
    detail: str


class CoverageOut(Schema):
    """``GET /objects/{id}/coverage`` 🔓.

    Alimenta el widget "a este objeto le faltan tomas desde el hemisferio sur":
    por eso además de las celdas se devuelve el reparto por hemisferio y los huecos
    ya interpretados, para que el frontend no replique la heurística.
    """

    object_id: UUID
    total_photos: int
    cells: list[CoverageCell]
    lat_band_size_deg: int
    #: Número de fotos por hemisferio del observador.
    northern_count: int
    southern_count: int
    #: Rango de focales presentes, mm.
    focal_min_mm: float | None = None
    focal_max_mm: float | None = None
    #: Diversidad de escala 0–1 medida sobre las fotos resueltas (ver domain.selection).
    scale_diversity: float = 0.0
    gaps: list[CoverageGap] = Field(default_factory=list)
