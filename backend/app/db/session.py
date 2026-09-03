"""Motor, sesión async y **unidad de trabajo por petición**.

El commit no puede vivir en el teardown de una dependencia: ahí corre cuando la
respuesta ya está en camino, así que el cliente recibe un 200 que promete un estado
que todavía no es visible para ninguna otra conexión. QA lo midió: 12 de cada 15
lecturas inmediatas tras ``POST /photos/{id}/complete`` devolvían la fila anterior.

Aquí la unidad de trabajo es **la petición**:

* se abre perezosamente (una petición que no toca la base no gasta conexión),
* se confirma cuando el handler termina con éxito, **antes** de emitir la respuesta,
* se revierte ante excepción o ante cualquier respuesta 4xx/5xx,
* y solo entonces se cierra.

Quien lo orquesta es :class:`app.core.uow.UnitOfWorkMiddleware`, no el teardown de
la dependencia, para que el orden respecto a la respuesta sea explícito y no dependa
de detalles internos de FastAPI.

Async de arriba abajo (regla dura 8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

from app.core.config import Settings, get_settings

__all__ = [
    "dispose_engine",
    "get_engine",
    "get_session",
    "session_factory",
    "session_scope",
]

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Motor único por proceso. Se crea perezosamente para no tocar la red al importar."""
    global _engine
    if _engine is None:
        cfg = settings or get_settings()
        kwargs: dict[str, Any] = {"echo": cfg.db_echo, "pool_pre_ping": True}
        if not cfg.database_url.startswith("sqlite"):
            kwargs["pool_size"] = cfg.db_pool_size
            kwargs["max_overflow"] = cfg.db_max_overflow
        _engine = create_async_engine(cfg.database_url, **kwargs)
    return _engine


def session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _factory


async def dispose_engine() -> None:
    """Cierra el pool. Se llama en el ``lifespan`` de la app."""
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None


async def get_session(request: Request) -> AsyncSession:
    """Dependencia FastAPI: la sesión de la unidad de trabajo de esta petición.

    **No tiene teardown a propósito.** El commit, el rollback y el cierre los hace
    :class:`app.core.uow.UnitOfWorkMiddleware` en un punto determinista —después del
    handler y antes de emitir la respuesta— en vez de en el desmontaje de la
    dependencia, que corre cuando la respuesta ya salió.

    Todas las dependencias y servicios de una misma petición comparten esta sesión,
    así que todo lo que escriben entra en la **misma transacción**: la fila de
    idempotencia se confirma junto al trabajo que protege, o no se confirma ninguna.
    """
    from app.core.uow import unit_of_work

    return await unit_of_work(request).session()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Sesión propia, fuera de la unidad de trabajo de una petición.

    Es lo que deben usar los workers, los scripts y **cualquier código que corra
    después de que la respuesta se haya emitido**: generadores de streaming (SSE) y
    ``BackgroundTasks``. Para entonces la sesión de la petición ya está cerrada, y
    usarla sería un ``IllegalStateChangeError`` en el mejor caso y una fuga de
    conexión en el peor.
    """
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
