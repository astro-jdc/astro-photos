"""``/healthz`` (liveness) y ``/readyz`` (DB + S3 + cola)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from app.api.deps import DbSession, SettingsDep, get_queue, get_storage
from app.schemas.common import HealthOut, ReadinessCheck, ReadinessOut
from app.services.queue import QueueService
from app.services.storage import StorageService

router = APIRouter(tags=["health"])

APP_VERSION = "0.1.0"


@router.get("/healthz", response_model=HealthOut, summary="Liveness")
async def healthz(settings: SettingsDep) -> HealthOut:
    """No toca dependencias: solo dice que el proceso responde."""
    return HealthOut(status="ok", version=APP_VERSION, environment=settings.environment)


@router.get("/readyz", response_model=ReadinessOut, summary="Readiness: DB + S3 + cola")
async def readyz(
    session: DbSession,
    response: Response,
    storage: Annotated[StorageService, Depends(get_storage)],
    queue: Annotated[QueueService, Depends(get_queue)],
) -> ReadinessOut:
    """Devuelve 503 si algo falta, para que el ALB saque la instancia del balanceo."""
    checks: list[ReadinessCheck] = []

    start = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        checks.append(
            ReadinessCheck(
                name="database",
                ok=True,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
            )
        )
    except Exception as exc:
        checks.append(ReadinessCheck(name="database", ok=False, detail=type(exc).__name__))

    start = time.perf_counter()
    s3_ok = await storage.healthy()
    checks.append(
        ReadinessCheck(
            name="s3",
            ok=s3_ok,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            detail=None if s3_ok else "el bucket de subidas no responde",
        )
    )

    start = time.perf_counter()
    queue_ok = await queue.healthy()
    checks.append(
        ReadinessCheck(
            name="queue",
            ok=queue_ok,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            detail=None if queue_ok else "la cola de ingesta no responde",
        )
    )

    all_ok = all(c.ok for c in checks)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessOut(
        status="ready" if all_ok else "degraded",
        checked_at=datetime.now(UTC),
        checks=checks,
    )
