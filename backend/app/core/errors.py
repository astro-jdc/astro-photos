"""Excepciones de dominio y handlers RFC 9457 (``application/problem+json``).

Regla dura 4 de ``.claude/agents/backend-dev.md``: nunca un 500 desnudo, nunca un
mensaje de excepción crudo al cliente. Todo error sale como

.. code-block:: json

    {"type": "...", "title": "...", "status": 422, "detail": "...",
     "instance": "/api/v1/...", "errors": [...]}
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "AppError",
    "BadRequestError",
    "ConflictError",
    "ForbiddenError",
    "LicenseBlockedError",
    "NotFoundError",
    "QuotaExceededError",
    "RateLimitError",
    "UnauthorizedError",
    "UnprocessableError",
    "UpstreamError",
    "install_error_handlers",
    "problem_response",
]

PROBLEM_CONTENT_TYPE = "application/problem+json"

#: Starlette renombró estas dos constantes; se fijan aquí para no depender de la
#: versión y para no arrastrar el DeprecationWarning por todo el paquete.
HTTP_413_CONTENT_TOO_LARGE = 413
HTTP_422_UNPROCESSABLE_CONTENT = 422

#: Base de los `type` URI. No se resuelve por red; es un identificador estable.
PROBLEM_TYPE_BASE = "https://astro-photos.dev/problems"

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Error de dominio con toda la información necesaria para un problem+json."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    problem_type: str = "internal-error"
    title: str = "Error interno"

    def __init__(
        self,
        detail: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []
        self.headers = headers or {}
        self.extra = extra or {}

    def to_problem(self, instance: str) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{PROBLEM_TYPE_BASE}/{self.problem_type}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
        }
        if self.errors:
            problem["errors"] = self.errors
        problem.update(self.extra)
        return problem


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    problem_type = "invalid-request"
    title = "Petición inválida"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    problem_type = "unauthorized"
    title = "Autenticación requerida"

    def __init__(self, detail: str = "Falta un token válido.", **kw: Any) -> None:
        headers = {"WWW-Authenticate": 'Bearer realm="astro-photos"'}
        headers.update(kw.pop("headers", {}) or {})
        super().__init__(detail, headers=headers, **kw)


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    problem_type = "forbidden"
    title = "Sin permiso"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    problem_type = "not-found"
    title = "No encontrado"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    problem_type = "conflict"
    title = "Conflicto de estado"


class UnprocessableError(AppError):
    status_code = HTTP_422_UNPROCESSABLE_CONTENT
    problem_type = "unprocessable"
    title = "No se puede procesar"


class LicenseBlockedError(UnprocessableError):
    """Regla 1 de ``docs/licensing.md``: el job se rechaza, no se degrada."""

    problem_type = "license-blocked"
    title = "Licencias incompatibles"


class QuotaExceededError(AppError):
    status_code = HTTP_413_CONTENT_TOO_LARGE
    problem_type = "quota-exceeded"
    title = "Cuota de almacenamiento superada"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    problem_type = "rate-limited"
    title = "Demasiadas peticiones"


class UpstreamError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    problem_type = "upstream-unavailable"
    title = "Servicio dependiente no disponible"


def problem_response(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    problem_type: str = "about:blank",
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Construye una respuesta ``application/problem+json``."""
    body: dict[str, Any] = {
        "type": problem_type
        if problem_type.startswith(("http://", "https://", "about:"))
        else f"{PROBLEM_TYPE_BASE}/{problem_type}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
    }
    if errors:
        body["errors"] = errors
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


_STATUS_TITLES: dict[int, str] = {
    400: "Petición inválida",
    401: "Autenticación requerida",
    403: "Sin permiso",
    404: "No encontrado",
    405: "Método no permitido",
    409: "Conflicto de estado",
    413: "Contenido demasiado grande",
    415: "Tipo de contenido no soportado",
    422: "No se puede procesar",
    429: "Demasiadas peticiones",
    500: "Error interno",
    502: "Servicio dependiente no disponible",
    503: "Servicio no disponible",
}


def install_error_handlers(app: FastAPI) -> None:
    """Registra los handlers. Llamar una vez al construir la app."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: Exception) -> JSONResponse:
        err = exc if isinstance(exc, AppError) else AppError(str(exc))
        if err.status_code >= 500:
            log.error(
                "app_error",
                problem_type=err.problem_type,
                detail=err.detail,
                path=request.url.path,
            )
        else:
            log.info(
                "app_error",
                problem_type=err.problem_type,
                status=err.status_code,
                path=request.url.path,
            )
        return JSONResponse(
            status_code=err.status_code,
            content=err.to_problem(request.url.path),
            media_type=PROBLEM_CONTENT_TYPE,
            headers=err.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: Exception) -> JSONResponse:
        raw = exc.errors() if isinstance(exc, RequestValidationError) else []
        errors = [
            {
                "pointer": "/" + "/".join(str(p) for p in e.get("loc", ())),
                "detail": str(e.get("msg", "")),
                "code": str(e.get("type", "")),
            }
            for e in raw
        ]
        return problem_response(
            request,
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            problem_type="invalid-request",
            title="Petición inválida",
            detail="Uno o más campos de la petición no son válidos.",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: Exception) -> JSONResponse:
        http_exc = exc if isinstance(exc, StarletteHTTPException) else StarletteHTTPException(500)
        code = http_exc.status_code
        return problem_response(
            request,
            status_code=code,
            problem_type=f"http-{code}",
            title=_STATUS_TITLES.get(code, "Error"),
            detail=str(http_exc.detail),
            headers=dict(http_exc.headers) if http_exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Nunca se filtra el mensaje de la excepción: va al log, no al cliente.
        log.exception("unhandled_exception", path=request.url.path, error=type(exc).__name__)
        return problem_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            problem_type="internal-error",
            title="Error interno",
            detail=(
                "Ha ocurrido un error inesperado. El identificador de la petición "
                "está en la cabecera X-Request-ID."
            ),
        )
