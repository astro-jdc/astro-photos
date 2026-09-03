"""App FastAPI de astro-photos."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import PROBLEM_CONTENT_TYPE, install_error_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db.session import dispose_engine
from app.schemas.common import ProblemDetail

__all__ = ["app", "create_app"]

log = structlog.get_logger(__name__)

DESCRIPTION = """
Repositorio colaborativo de astrofotografía.

Los usuarios suben tomas con metadata rica y el sistema combina muchas tomas de
muchos observadores en una imagen más profunda y mejor muestreada.

**Lo que el sistema gana al combinar**: relación señal/ruido, muestreo sub-píxel,
rango dinámico e información temporal. **Lo que no gana**: resolución angular más
allá del límite de difracción de la mejor óptica contribuyente. Combinar fotos de
observadores separados no sintetiza una apertura.

Errores en formato RFC 9457 (`application/problem+json`).
"""

#: Respuestas de error comunes, para que salgan en el OpenAPI y el frontend genere
#: tipos correctos (regla dura 7 de CLAUDE.md).
COMMON_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {
        "model": ProblemDetail,
        "description": "Petición inválida",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    401: {
        "model": ProblemDetail,
        "description": "Autenticación requerida",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    403: {
        "model": ProblemDetail,
        "description": "Sin permiso",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    404: {
        "model": ProblemDetail,
        "description": "No encontrado",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    409: {
        "model": ProblemDetail,
        "description": "Conflicto de estado",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    422: {
        "model": ProblemDetail,
        "description": "No se puede procesar",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    429: {
        "model": ProblemDetail,
        "description": "Demasiadas peticiones",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
    500: {
        "model": ProblemDetail,
        "description": "Error interno",
        "content": {PROBLEM_CONTENT_TYPE: {}},
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranque y parada. No se crean tablas aquí: eso es trabajo de Alembic."""
    settings: Settings = app.state.settings
    log.info(
        "startup",
        environment=settings.environment,
        auth_mode=settings.auth_mode,
        api_prefix=settings.api_prefix,
    )
    try:
        yield
    finally:
        await dispose_engine()
        log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construye la app. Recibe ``settings`` para poder instanciarla en tests."""
    cfg = settings or get_settings()
    configure_logging(cfg.log_level, json_logs=cfg.environment != "dev")

    app = FastAPI(
        title="astro-photos API",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_version="3.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"{cfg.api_prefix}/openapi.json",
        responses=COMMON_RESPONSES,
    )
    app.state.settings = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Location", "X-Attribution", "X-License"],
    )
    # GZip después de CORS para que la cabecera de CORS no acabe comprimida por error.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestContextMiddleware)

    install_error_handlers(app)
    app.include_router(api_router, prefix=cfg.api_prefix)
    return app


app = create_app()
