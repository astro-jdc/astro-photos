"""App FastAPI de astro-photos."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi

from app.api.v1.router import V1_ROUTERS, api_router
from app.core.config import Settings, get_settings
from app.core.errors import PROBLEM_CONTENT_TYPE, install_error_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.uow import install_unit_of_work
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


def _uses_only_optional_auth(route: Any) -> bool:
    """¿Esta ruta acepta anónimos aunque sepa leer un Bearer si viene?

    Recorre el árbol de dependencias buscando `current_user` (obligatorio) y
    `optional_user` (opcional). Se hace por inspección y no con una lista escrita a
    mano para que una ruta nueva no se quede fuera por olvido.
    """
    from app.core.security import current_user, optional_user

    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False

    found_optional = False
    stack = [dependant]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.call is current_user:
            return False
        if node.call is optional_user:
            found_optional = True
        stack.extend(node.dependencies)
    return found_optional


def _mark_optional_security(schema: dict[str, Any], prefix: str) -> None:
    """Declara como **opcional** la autenticación de las rutas 🔓.

    FastAPI anota el esquema Bearer en cuanto una ruta sabe leerlo, aunque sea con
    `auto_error=False`. El resultado es un OpenAPI que dice "hace falta token" en
    rutas públicas, y los clientes generados a partir de él exigen credenciales que
    nadie pide. El idioma de OpenAPI para "opcional" es incluir el requisito vacío
    `{}` junto al esquema: cualquiera de los dos sirve.
    """
    paths = schema.get("paths", {})
    for router in V1_ROUTERS:
        for route in router.routes:
            _mark_route(paths, prefix, route)


def _mark_route(paths: dict[str, Any], prefix: str, route: Any) -> None:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None)
    if not path or not methods or not _uses_only_optional_auth(route):
        return
    operations = paths.get(f"{prefix}{path}")
    if operations:
        for method in methods:
            operation = operations.get(method.lower())
            if operation is None:
                continue
            security = operation.get("security")
            if security and {} not in security:
                operation["security"] = [{}, *security]


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

    # El **más interno** de todos, y por eso se añade primero: envuelve solo la
    # ejecución del handler, así que confirma la transacción justo antes de que el
    # `http.response.start` salga hacia el cliente. Ninguna otra capa puede colarse
    # entre el commit y la respuesta.
    install_unit_of_work(app)

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

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
        _mark_optional_security(schema, cfg.api_prefix)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


app = create_app()
