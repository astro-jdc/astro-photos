"""Objetos del cielo y mapa de cobertura."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict

from app.core.errors import NotFoundError
from app.domain.location import GeoPoint, LocationPrecision, obfuscate_location
from app.models.sky_object import SkyObject
from app.repositories.sky_object import (
    FOCAL_BANDS_MM,
    LAT_BAND_DEG,
    PERIOD_BIN,
    UNKNOWN_LAT_BIN,
    CoverageRow,
    RawSiteRow,
    SkyObjectRepository,
)
from app.schemas.sky_object import (
    CoverageCell,
    CoverageGap,
    CoverageOut,
    CoverageSite,
    ObjectOut,
)

__all__ = ["ObjectService", "build_sites"]

#: Umbral por debajo del cual un hemisferio se considera "sin cubrir" respecto del
#: otro. 20 % es el punto en que la asimetría empieza a notarse en la cobertura de
#: ángulo paraláctico y de ventana horaria.
HEMISPHERE_GAP_RATIO = 0.2

#: Orden de grosor de las precisiones: al fundir puntos gana la más gruesa.
_COARSENESS = {
    LocationPrecision.EXACT: 0,
    LocationPrecision.CITY: 1,
    LocationPrecision.COUNTRY: 2,
    LocationPrecision.HIDDEN: 3,
}


def build_sites(rows: list[RawSiteRow]) -> list[CoverageSite]:
    """Convierte posiciones crudas en puntos publicables.

    **El orden es la garantía de privacidad**: se ofusca cada grupo con *su propia*
    precisión y solo después se agregan los que caen en el mismo punto. Al revés
    —agregar y ofuscar el resultado— bastaría con que una sola foto exacta cayera en
    el grupo para que el punto publicado delatase el sitio, que es justo el fallo
    que la regla de privacidad existe para evitar.

    Cuando dos grupos de precisiones distintas acaban en el mismo punto publicado,
    el punto se etiqueta con la precisión **más gruesa** de los dos: es la única
    afirmación que se puede sostener sobre él.
    """
    merged: dict[tuple[float, float], tuple[int, LocationPrecision]] = defaultdict(
        lambda: (0, LocationPrecision.EXACT)
    )
    for row in rows:
        published = obfuscate_location(
            GeoPoint(
                lat=row.lat,
                lon=row.lon,
                accuracy_m=row.accuracy_m,
                elevation_m=row.elevation_m,
                country_code=row.country_code,
            ),
            row.precision,
        )
        # `None` = no publicable (hidden, o country sin país conocido). No se
        # aproxima: se deja fuera del mapa.
        if published is None or published.lat is None or published.lon is None:
            continue
        key = (round(published.lat, 6), round(published.lon, 6))
        count, precision = merged[key]
        coarsest = max(precision, row.precision, key=lambda p: _COARSENESS[p])
        merged[key] = (count + row.count, coarsest)

    return [
        CoverageSite(lat=lat, lon=lon, count=count, precision=precision.value)
        # Orden estable: el mismo objeto siempre pinta el mapa igual.
        for (lat, lon), (count, precision) in sorted(merged.items())
    ]


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
        """``GET /objects/{id}/coverage`` — histograma, sitios y huecos.

        Los huecos se calculan aquí y no en el frontend para que la heurística viva
        en un solo sitio y sea testeable.
        """
        obj = await self.get(object_id)
        rows: list[CoverageRow] = await self.objects.coverage(object_id)
        sites = build_sites(await self.objects.raw_sites(object_id))

        cells = [
            CoverageCell(
                period=r.period,
                lat_bin=r.lat_bin,
                focal_bin=r.focal_bin,
                count=r.count,
                best_quality=r.best_quality,
            )
            for r in rows
        ]
        total = sum(r.count for r in rows)
        located = [r for r in rows if r.lat_bin != UNKNOWN_LAT_BIN]
        north = sum(r.count for r in located if r.lat_bin >= 0)
        south = sum(r.count for r in located if r.lat_bin < 0)

        focals = sorted({r.focal_bin for r in rows if r.focal_bin > 0})
        focal_min = float(focals[0]) if focals else None
        focal_max = float(focals[-1]) if focals else None
        # Diversidad de escala: octavas cubiertas, saturadas en un factor 4 (mismo
        # criterio que domain.selection, para que los dos números sean comparables).
        scale_diversity = 0.0
        if focal_min and focal_max and focal_min > 0:
            scale_diversity = min(1.0, math.log2(focal_max / focal_min) / 2.0)

        gaps = self._gaps(
            designation=obj.designation,
            total=total,
            north=north,
            south=south,
            scale_diversity=scale_diversity,
            periods={r.period for r in rows if r.period != "unknown"},
        )

        return CoverageOut(
            object_id=object_id,
            total_photos=total,
            period_bin=PERIOD_BIN,
            lat_bin_size_deg=LAT_BAND_DEG,
            focal_bins_mm=list(FOCAL_BANDS_MM),
            cells=cells,
            sites=sites,
            gaps=gaps,
            northern_count=north,
            southern_count=south,
            focal_min_mm=focal_min,
            focal_max_mm=focal_max,
            scale_diversity=scale_diversity,
        )

    @staticmethod
    def _gaps(
        *,
        designation: str,
        total: int,
        north: int,
        south: int,
        scale_diversity: float,
        periods: set[str],
    ) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        located = north + south
        if located > 0:
            if south < located * HEMISPHERE_GAP_RATIO:
                gaps.append(
                    CoverageGap(
                        reason="hemisphere",
                        description=(
                            "A este objeto le faltan tomas desde el hemisferio sur: "
                            f"{south} de {located} con posición conocida."
                        ),
                    )
                )
            elif north < located * HEMISPHERE_GAP_RATIO:
                gaps.append(
                    CoverageGap(
                        reason="hemisphere",
                        description=(
                            "A este objeto le faltan tomas desde el hemisferio norte: "
                            f"{north} de {located} con posición conocida."
                        ),
                    )
                )
        if scale_diversity < 0.25 and total > 0:
            gaps.append(
                CoverageGap(
                    reason="focal",
                    description=(
                        "Casi todas las tomas usan focales parecidas. Aportar otra "
                        "escala mejora la recuperación de muestreo, no solo la SNR."
                    ),
                )
            )
        if 0 < len(periods) < 3:
            gaps.append(
                CoverageGap(
                    reason="temporal",
                    description=(
                        f"Solo hay {len(periods)} mes(es) con datos: poca diversidad "
                        "temporal para separar variabilidad de artefactos."
                    ),
                )
            )
        if total == 0:
            gaps.append(
                CoverageGap(
                    reason="empty",
                    description=f"Todavía no hay ninguna foto de {designation}.",
                )
            )
        return gaps
