"""Motor y sesión async. Async de arriba abajo (regla dura 8)."""

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


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia FastAPI: una sesión por petición, commit/rollback automáticos."""
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Sesión fuera de una petición HTTP (workers, scripts)."""
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
