"""Objetos del cielo y mapa de cobertura."""

from __future__ import annotations

import math
import uuid

from app.core.errors import NotFoundError
from app.models.sky_object import SkyObject
from app.repositories.sky_object import LAT_BAND_DEG, CoverageRow, SkyObjectRepository
from app.schemas.sky_object import CoverageCell, CoverageGap, CoverageOut, ObjectOut

__all__ = ["ObjectService"]

#: Umbral por debajo del cual un hemisferio se considera "sin cubrir" respecto del
#: otro. 20 % es el punto en que la asimetría empieza a notarse en la cobertura de
#: ángulo paraláctico y de ventana horaria.
HEMISPHERE_GAP_RATIO = 0.2


class ObjectService:
    def __init__(self, objects: SkyObjectRepository) -> None:
        self.objects = objects

    @staticmethod
    def to_out(obj: SkyObject) -> ObjectOut:
        return ObjectOut(
            id=obj.id,
            catalog=obj.catalog,
            catalog_number=obj.catalog_number,
            designation=obj.designation,
            common_name=obj.common_name,
            common_name_es=obj.common_name_es,
            object_type=obj.object_type,
            ra_deg=obj.ra_deg,
            dec_deg=obj.dec_deg,
            magnitude=obj.magnitude,
            size_arcmin=obj.size_arcmin,
            aliases=list(obj.aliases or []),
            is_ephemeral=obj.is_ephemeral,
            photo_count=obj.photo_count,
            reconstruction_count=obj.reconstruction_count,
        )

    async def get(self, object_id: uuid.UUID) -> SkyObject:
        obj = await self.objects.get(object_id)
        if obj is None:
            raise NotFoundError("El objeto no existe en el catálogo.")
        return obj

    async def coverage(self, object_id: uuid.UUID) -> CoverageOut:
        """``GET /objects/{id}/coverage`` — histograma + huecos ya interpretados.

        Los huecos se calculan aquí y no en el frontend para que la heurística viva
        en un solo sitio y sea testeable.
        """
        obj = await self.get(object_id)
        rows: list[CoverageRow] = await self.objects.coverage(object_id)

        cells = [
            CoverageCell(
                month=r.month,
                lat_band_deg=r.lat_band_deg,
                focal_band_mm=r.focal_band_mm,
                photo_count=r.photo_count,
                mean_quality=r.mean_quality,
            )
            for r in rows
        ]
        total = sum(r.photo_count for r in rows)
        north = sum(r.photo_count for r in rows if r.lat_band_deg >= 0 and r.lat_band_deg > -900)
        south = sum(r.photo_count for r in rows if -900 < r.lat_band_deg < 0)

        focals = sorted({r.focal_band_mm for r in rows if r.focal_band_mm > 0})
        focal_min = float(focals[0]) if focals else None
        focal_max = float(focals[-1]) if focals else None
        # Diversidad de escala: octavas cubiertas, saturadas en un factor 4 (igual
        # criterio que domain.selection, para que los dos números sean comparables).
        scale_diversity = 0.0
        if focal_min and focal_max and focal_min > 0:
            scale_diversity = min(1.0, math.log2(focal_max / focal_min) / 2.0)

        gaps: list[CoverageGap] = []
        located = north + south
        if located > 0:
            if south < located * HEMISPHERE_GAP_RATIO:
                gaps.append(
                    CoverageGap(
                        kind="hemisphere",
                        detail=(
                            "A este objeto le faltan tomas desde el hemisferio sur: "
                            f"{south} de {located} con posición conocida."
                        ),
                    )
                )
            elif north < located * HEMISPHERE_GAP_RATIO:
                gaps.append(
                    CoverageGap(
                        kind="hemisphere",
                        detail=(
                            "A este objeto le faltan tomas desde el hemisferio norte: "
                            f"{north} de {located} con posición conocida."
                        ),
                    )
                )
        if scale_diversity < 0.25 and total > 0:
            gaps.append(
                CoverageGap(
                    kind="focal",
                    detail=(
                        "Casi todas las tomas usan focales parecidas. Aportar otra "
                        "escala mejora la recuperación de muestreo, no solo la SNR."
                    ),
                )
            )
        months = {r.month for r in rows if r.month != "unknown"}
        if 0 < len(months) < 3:
            gaps.append(
                CoverageGap(
                    kind="temporal",
                    detail=(
                        f"Solo hay {len(months)} mes(es) con datos: poca diversidad "
                        "temporal para separar variabilidad de artefactos."
                    ),
                )
            )
        if total == 0:
            gaps.append(
                CoverageGap(
                    kind="empty",
                    detail=f"Todavía no hay ninguna foto de {obj.designation}.",
                )
            )

        return CoverageOut(
            object_id=object_id,
            total_photos=total,
            cells=cells,
            lat_band_size_deg=LAT_BAND_DEG,
            northern_count=north,
            southern_count=south,
            focal_min_mm=focal_min,
            focal_max_mm=focal_max,
            scale_diversity=scale_diversity,
            gaps=gaps,
        )
