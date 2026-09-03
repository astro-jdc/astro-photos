"""Logging estructurado JSON con ``structlog`` y correlación por ``X-Request-ID``."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

__all__ = ["REQUEST_ID_HEADER", "RequestContextMiddleware", "configure_logging"]

REQUEST_ID_HEADER = "X-Request-ID"


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Configura structlog y el logging estándar para que compartan formato.

    En producción todo sale como JSON de una línea (CloudWatch lo indexa tal cual);
    en local con ``json_logs=False`` sale coloreado y legible.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric, force=True)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Propaga un ``X-Request-ID`` por petición y lo ata al contexto de log.

    Si el cliente (o el ALB) ya manda uno, se respeta: así una traza cruza el
    frontend, el backend y los workers con el mismo identificador.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._log = structlog.get_logger("http")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            # El handler global de errores formatea la respuesta; aquí solo se
            # garantiza que la cabecera de correlación exista en el log.
            self._log.exception("request_failed")
            raise
        response.headers[REQUEST_ID_HEADER] = request_id
        self._log.info("request_completed", status_code=response.status_code)
        return response
