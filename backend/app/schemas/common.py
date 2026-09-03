"""Tipos compartidos: paginación por cursor opaco, problem+json y ubicación."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.location import LocationPrecision, ObfuscatedLocation

__all__ = [
    "Cursor",
    "HealthOut",
    "LocationIn",
    "LocationOut",
    "Page",
    "ProblemDetail",
    "ReadinessCheck",
    "ReadinessOut",
    "Schema",
    "decode_cursor",
    "encode_cursor",
]


class Schema(BaseModel):
    """Base de todos los schemas: inmutable y estricta con campos desconocidos."""

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        populate_by_name=True,
        ser_json_timedelta="float",
    )


# --------------------------------------------------------------------------- #
# Paginación por cursor opaco
# --------------------------------------------------------------------------- #
Cursor = Annotated[str, Field(description="Cursor opaco devuelto por la página anterior")]


def encode_cursor(payload: dict[str, Any]) -> str:
    """Serializa un cursor a base64url. **Opaco por contrato**: el cliente no lo
    interpreta y nosotros podemos cambiar su contenido sin romper a nadie."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Inverso de :func:`encode_cursor`. Lanza ``ValueError`` si está corrupto."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception as exc:
        raise ValueError("Cursor inválido") from exc
    if not isinstance(data, dict):
        raise ValueError("Cursor inválido")
    return data


class Page[T](BaseModel):
    """``{items: [...], next_cursor: str|null}`` (``docs/api.md``)."""

    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    next_cursor: str | None = None
    #: Total aproximado cuando calcularlo es barato; ``None`` si no se calculó.
    total: int | None = None


# --------------------------------------------------------------------------- #
# RFC 9457
# --------------------------------------------------------------------------- #
class ProblemError(BaseModel):
    pointer: str | None = None
    detail: str
    code: str | None = None


class ProblemDetail(BaseModel):
    """Cuerpo ``application/problem+json``. Se declara para que salga en el OpenAPI."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    errors: list[ProblemError] | None = None


# --------------------------------------------------------------------------- #
# Ubicación
# --------------------------------------------------------------------------- #
class LocationIn(Schema):
    """Ubicación tal como la manda el cliente en ``POST /photos/{id}/complete``."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    accuracy_m: float | None = Field(default=None, ge=0.0)
    elevation_m: float | None = Field(default=None, ge=-500.0, le=9000.0)


class LocationOut(Schema):
    """Ubicación **ya ofuscada**. Nunca se construye a mano: sale de
    :func:`app.domain.location.obfuscate_location`."""

    lat: float | None
    lon: float | None
    accuracy_m: float | None
    elevation_m: float | None
    precision: LocationPrecision
    country_code: str | None = None

    @classmethod
    def from_domain(cls, value: ObfuscatedLocation | None) -> LocationOut | None:
        if value is None:
            return None
        return cls(
            lat=value.lat,
            lon=value.lon,
            accuracy_m=value.accuracy_m,
            elevation_m=value.elevation_m,
            precision=value.precision,
            country_code=value.country_code,
        )


class HealthOut(Schema):
    """``GET /healthz``."""

    status: str
    version: str
    environment: str


class ReadinessCheck(Schema):
    name: str
    ok: bool
    detail: str | None = None
    latency_ms: float | None = None


class ReadinessOut(Schema):
    """``GET /readyz`` — DB + S3 + cola."""

    status: str
    checked_at: datetime
    checks: list[ReadinessCheck]
