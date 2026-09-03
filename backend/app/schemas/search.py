"""Filtros de ``GET /photos`` y del ``selector`` de las reconstrucciones.

Un único objeto de filtros para los dos sitios: ``docs/api.md`` dice explícitamente
que el ``selector`` de ``POST /reconstructions`` usa "la misma sintaxis que la
búsqueda", así que compartirlo es la forma de que no diverjan.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.licensing import LicenseCode
from app.schemas.common import Schema

__all__ = ["GeoNear", "PhotoSearchQuery", "SortOrder", "UsableFor"]


class SortOrder(StrEnum):
    QUALITY = "quality"
    RECENT = "recent"
    NEAREST = "nearest"


class UsableFor(StrEnum):
    """Atajo de licencias: "solo lo que puedo usar para esto"."""

    COMMERCIAL = "commercial"
    DERIVATIVES = "derivatives"
    STACKING = "stacking"


class GeoNear(Schema):
    """``&near=28.30,-16.51&km=50``."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    km: float = Field(gt=0.0, le=20_000.0)


class PhotoSearchQuery(Schema):
    """Filtros combinables de ``GET /photos``.

    Los nombres coinciden con los parámetros de query del contrato; el router los
    recoge uno a uno y construye este objeto, que es lo que ve el repositorio.
    """

    #: Alias o id del objeto (``?object=M31``).
    object: str | None = None
    object_id: UUID | None = None

    #: Cono en el cielo, grados.
    ra: float | None = Field(default=None, ge=0.0, lt=360.0)
    dec: float | None = Field(default=None, ge=-90.0, le=90.0)
    radius: float | None = Field(default=None, gt=0.0, le=180.0)

    near: GeoNear | None = None

    date_from: date | None = Field(default=None, alias="from")
    date_to: date | None = Field(default=None, alias="to")

    min_focal: float | None = Field(default=None, gt=0.0)
    max_focal: float | None = Field(default=None, gt=0.0)
    filter: str | None = None

    #: Lista explícita de licencias aceptables.
    license: list[LicenseCode] | None = None
    usable_for: UsableFor | None = None

    min_quality: float | None = Field(default=None, ge=0.0, le=1.0)
    tracked: bool | None = None
    #: ``?owner=<uuid>`` en el contrato; ``owner_id`` se acepta como sinónimo.
    owner_id: UUID | None = Field(default=None, alias="owner")
    #: Solo fotos con astrometría resuelta (obligatorio para reconstruir).
    plate_solved: bool | None = None

    sort: SortOrder = SortOrder.QUALITY
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: str | None = None

    @field_validator("license", mode="before")
    @classmethod
    def _split_licenses(cls, value: Any) -> Any:
        """Acepta ``?license=CC-BY-4.0,CC0-1.0`` además de repetir el parámetro."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        cone = (self.ra is not None, self.dec is not None, self.radius is not None)
        if any(cone) and not all(cone):
            raise ValueError("El cono en el cielo exige ra, dec y radius a la vez.")
        if self.min_focal and self.max_focal and self.min_focal > self.max_focal:
            raise ValueError("min_focal no puede ser mayor que max_focal.")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("`from` no puede ser posterior a `to`.")
        if self.sort is SortOrder.NEAREST and self.near is None:
            raise ValueError("sort=nearest exige el parámetro `near`.")
        return self

    def allowed_licenses(self) -> list[LicenseCode] | None:
        """Traduce ``usable_for`` a un conjunto de licencias, o ``None`` si no filtra.

        Se apoya en el catálogo de ``domain.licensing``: el shortcut no puede
        divergir de los flags reales.
        """
        from app.domain.licensing import LICENSE_CATALOG

        explicit = set(self.license or [])
        derived: set[LicenseCode] | None = None
        if self.usable_for is UsableFor.COMMERCIAL:
            derived = {i.code for i in LICENSE_CATALOG if i.allows_commercial}
        elif self.usable_for in (UsableFor.DERIVATIVES, UsableFor.STACKING):
            derived = {i.code for i in LICENSE_CATALOG if i.allows_derivatives}

        if derived is None:
            return sorted(explicit) or None
        if explicit:
            return sorted(explicit & derived)
        return sorted(derived)
