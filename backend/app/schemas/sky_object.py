"""Schemas de ``/objects`` y del mapa de cobertura."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.models.enums import ObjectCatalog, ObjectType
from app.schemas.common import Schema

__all__ = [
    "CoverageCell",
    "CoverageGap",
    "CoverageOut",
    "CoverageSite",
    "ObjectOut",
]


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

    #: Bin temporal en formato ``YYYY-MM`` (ver ``period_bin``).
    period: str
    #: Límite inferior de la banda de latitud del observador, en grados.
    lat_bin: int
    #: Límite inferior de la banda de focal, en mm.
    focal_bin: int
    count: int
    #: La **mejor** calidad de la celda, no la media: lo que dice si falta cobertura
    #: aquí es si existe *alguna* toma buena, no el promedio.
    best_quality: float | None = None


class CoverageSite(Schema):
    """Un punto del mapa de sitios, **ya ofuscado**.

    Nunca es más preciso que lo que autorizó la foto menos precisa que lo compone:
    la ofuscación se aplica por foto **antes** de agregar. Agregar y ofuscar después
    filtraría posiciones exactas por la puerta de atrás.
    """

    lat: float
    lon: float
    count: int
    #: La precisión más gruesa entre las fotos que componen el punto.
    precision: str


class CoverageGap(Schema):
    """Un hueco detectado, en texto listo para el widget."""

    reason: str
    description: str


class CoverageOut(Schema):
    """``GET /objects/{id}/coverage`` 🔓.

    Alimenta el widget "a este objeto le faltan tomas desde el hemisferio sur": por
    eso, además de las celdas, se devuelven los huecos ya interpretados, para que el
    frontend no replique la heurística.
    """

    object_id: UUID
    total_photos: int
    #: Tamaño del bin temporal (``month``).
    period_bin: str
    lat_bin_size_deg: int
    #: Bordes de las bandas de focal, en mm.
    focal_bins_mm: list[int] = Field(default_factory=list)
    cells: list[CoverageCell] = Field(default_factory=list)
    sites: list[CoverageSite] = Field(default_factory=list)
    gaps: list[CoverageGap] = Field(default_factory=list)

    #: Número de fotos por hemisferio del observador.
    northern_count: int = 0
    southern_count: int = 0
    focal_min_mm: float | None = None
    focal_max_mm: float | None = None
    #: Diversidad de escala 0–1 sobre las fotos resueltas (ver ``domain.selection``).
    scale_diversity: float = 0.0
