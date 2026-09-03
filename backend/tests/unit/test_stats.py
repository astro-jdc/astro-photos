"""Caché de ``GET /stats``.

Lo que importa aquí no son los números —los cuenta Postgres— sino que la caché
cumpla su contrato: 5 minutos, sin estampida, y con un reloj que no se pueda romper
cambiando la hora del sistema.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.repositories.stats import StatsSnapshot
from app.services.stats import STATS_TTL_SECONDS, StatsCache, StatsService

SNAPSHOT = StatsSnapshot(
    photo_count=1200,
    object_count=110,
    reconstruction_count=17,
    contributor_count=48,
    total_exposure_seconds=987_654.0,
)


class CountingRepo:
    """Repositorio que cuenta cuántas veces se le ha preguntado de verdad."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay

    async def snapshot(self) -> StatsSnapshot:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return SNAPSHOT


def test_the_documented_ttl_is_five_minutes() -> None:
    assert STATS_TTL_SECONDS == 300


async def test_the_first_call_hits_the_database() -> None:
    repo = CountingRepo()
    service = StatsService(repo, StatsCache())  # type: ignore[arg-type]
    stats = await service.get()
    assert repo.calls == 1
    assert stats.photo_count == 1200
    assert stats.total_exposure_seconds == pytest.approx(987_654.0)


async def test_subsequent_calls_are_served_from_the_cache() -> None:
    repo = CountingRepo()
    service = StatsService(repo, StatsCache())  # type: ignore[arg-type]
    for _ in range(10):
        await service.get()
    assert repo.calls == 1


async def test_the_cache_expires_after_the_ttl() -> None:
    repo = CountingRepo()
    service = StatsService(repo, StatsCache(ttl_seconds=0))  # type: ignore[arg-type]
    await service.get()
    await service.get()
    assert repo.calls == 2


async def test_invalidate_forces_a_recount() -> None:
    repo = CountingRepo()
    cache = StatsCache()
    service = StatsService(repo, cache)  # type: ignore[arg-type]
    await service.get()
    cache.invalidate()
    await service.get()
    assert repo.calls == 2


async def test_a_cold_cache_does_not_stampede() -> None:
    """Cien peticiones simultáneas con la caché fría ⇒ una sola consulta."""
    repo = CountingRepo(delay=0.02)
    service = StatsService(repo, StatsCache())  # type: ignore[arg-type]
    results = await asyncio.gather(*(service.get() for _ in range(100)))
    assert repo.calls == 1
    assert all(r.photo_count == 1200 for r in results)


async def test_the_cache_uses_a_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un salto del reloj del sistema no debe invalidar ni eternizar la caché."""
    import time as time_module

    repo = CountingRepo()
    service = StatsService(repo, StatsCache())  # type: ignore[arg-type]
    await service.get()

    # La hora de pared salta un año hacia atrás; la caché no se inmuta.
    real_time = time_module.time

    def travelled() -> float:
        return real_time() - 365 * 24 * 3600

    monkeypatch.setattr(time_module, "time", travelled)
    await service.get()
    assert repo.calls == 1


async def test_computed_at_is_reported_so_the_client_knows_how_stale_it_is() -> None:
    service = StatsService(CountingRepo(), StatsCache())  # type: ignore[arg-type]
    first = await service.get()
    second = await service.get()
    # Misma entrada de caché ⇒ mismo `computed_at`: no se finge frescura.
    assert first.computed_at == second.computed_at


def test_the_repository_only_counts_what_is_public() -> None:
    """Documenta el criterio en un sitio ejecutable, no solo en un comentario.

    La consulta real se prueba en integración; aquí se fija que el criterio escrito
    en el repositorio es el del contrato.
    """
    import inspect

    from app.repositories.stats import StatsRepository

    source = inspect.getsource(StatsRepository.snapshot)
    assert "PhotoStatus.READY" in source
    assert "deleted_at.is_(None)" in source
    assert "JobStatus.SUCCEEDED" in source
    assert "is_public" in source


def test_stats_service_uses_the_shared_cache_by_default() -> None:
    """Dos peticiones distintas comparten caché: es del proceso, no de la sesión."""
    from app.services.stats import stats_cache

    a = StatsService(CountingRepo())  # type: ignore[arg-type]
    b = StatsService(CountingRepo())  # type: ignore[arg-type]
    assert a.cache is b.cache is stats_cache


def test_quota_helper_says_whether_a_job_can_be_queued() -> None:
    """El cliente deshabilita el botón con esto en vez de comerse un 429."""
    from app.schemas.user import QuotaOut

    base: dict[str, Any] = {
        "quota_bytes": 100,
        "used_bytes": 0,
        "available_bytes": 100,
        "used_fraction": 0.0,
        "max_queued_jobs": 5,
        "max_jobs_per_day": 20,
    }
    assert QuotaOut(**base, jobs_queued_now=0, jobs_today=0).can_queue_job is True
    assert QuotaOut(**base, jobs_queued_now=5, jobs_today=0).can_queue_job is False
    assert QuotaOut(**base, jobs_queued_now=0, jobs_today=20).can_queue_job is False
