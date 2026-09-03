"""Estadísticas de portada, con caché en proceso.

Cinco `COUNT(*)` sobre tablas grandes en cada carga de la home no tienen sentido:
son números que cambian por minutos, no por segundos. Se cachean 5 minutos en
memoria del proceso (``docs/api.md``); con varias réplicas cada una tiene la suya y
pueden ir desfasadas unos minutos entre sí, lo cual es irrelevante para un contador
de portada y evita meter Redis en el camino.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.repositories.stats import StatsRepository, StatsSnapshot
from app.schemas.stats import StatsOut

__all__ = ["STATS_TTL_SECONDS", "StatsCache", "StatsService", "stats_cache"]

log = structlog.get_logger(__name__)

#: Vida de la caché, en segundos (``docs/api.md``: "Cacheado 5 minutos").
STATS_TTL_SECONDS = 300


@dataclass(slots=True)
class _Entry:
    snapshot: StatsSnapshot
    computed_at: datetime
    monotonic: float


class StatsCache:
    """Caché de un solo valor con TTL, segura entre corrutinas.

    El ``Lock`` evita la estampida: si llegan cien peticiones con la caché fría, una
    sola consulta la base y las otras noventa y nueve esperan a esa.
    """

    def __init__(self, ttl_seconds: int = STATS_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entry: _Entry | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> _Entry | None:
        entry = self._entry
        if entry is None:
            return None
        # `monotonic` y no `now()`: un salto del reloj del sistema no debe
        # invalidar ni eternizar la caché.
        if time.monotonic() - entry.monotonic > self._ttl:
            return None
        return entry

    async def get(self, repository: StatsRepository) -> _Entry:
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock:
            # Otra corrutina puede haberla rellenado mientras esperábamos el lock.
            cached = self._fresh()
            if cached is not None:
                return cached
            snapshot = await repository.snapshot()
            entry = _Entry(
                snapshot=snapshot,
                computed_at=datetime.now(UTC),
                monotonic=time.monotonic(),
            )
            self._entry = entry
            log.info("stats_recomputed", photo_count=snapshot.photo_count)
            return entry

    def invalidate(self) -> None:
        """Vacía la caché. Para tests y para el seed de desarrollo."""
        self._entry = None


#: Caché compartida por el proceso.
stats_cache = StatsCache()


class StatsService:
    def __init__(self, repository: StatsRepository, cache: StatsCache | None = None) -> None:
        self.repository = repository
        self.cache = cache if cache is not None else stats_cache

    async def get(self) -> StatsOut:
        entry = await self.cache.get(self.repository)
        snapshot = entry.snapshot
        return StatsOut(
            photo_count=snapshot.photo_count,
            object_count=snapshot.object_count,
            reconstruction_count=snapshot.reconstruction_count,
            contributor_count=snapshot.contributor_count,
            total_exposure_seconds=snapshot.total_exposure_seconds,
            computed_at=entry.computed_at,
        )
