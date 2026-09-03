"""``GET /stats`` 🔓 — contadores de la portada."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_stats_service
from app.schemas.stats import StatsOut
from app.services.stats import STATS_TTL_SECONDS, StatsService

router = APIRouter(tags=["stats"])


@router.get("/stats", response_model=StatsOut, summary="Contadores del repositorio")
async def read_stats(
    response: Response,
    service: Annotated[StatsService, Depends(get_stats_service)],
) -> StatsOut:
    """Fotos, objetos, reconstrucciones, contribuyentes y exposición acumulada.

    Cuenta sólo lo publicable: fotos ``ready`` sin borrado lógico y reconstrucciones
    ``succeeded`` y públicas. Cacheado 5 minutos en proceso.
    """
    stats = await service.get()
    # La misma vida que la caché del servidor: así un CDN o el navegador no piden
    # más a menudo de lo que el número puede cambiar.
    response.headers["Cache-Control"] = f"public, max-age={STATS_TTL_SECONDS}"
    return stats
