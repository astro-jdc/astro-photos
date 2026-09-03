"""Utilidades comunes de repositorio: paginación por cursor opaco."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.errors import BadRequestError
from app.schemas.common import decode_cursor, encode_cursor

__all__ = ["CursorPage", "KeysetCursor", "parse_cursor"]


@dataclass(frozen=True, slots=True)
class KeysetCursor:
    """Cursor de keyset: el valor de ordenación y el id de desempate.

    Keyset en vez de OFFSET porque la galería es grande y cambia mientras se
    pagina; con OFFSET se repiten y se pierden filas.
    """

    sort_value: Any
    last_id: str

    def encode(self) -> str:
        return encode_cursor({"v": self.sort_value, "id": self.last_id})


def parse_cursor(cursor: str | None) -> KeysetCursor | None:
    """Decodifica el cursor o lanza un 400 en problem+json si está corrupto."""
    if not cursor:
        return None
    try:
        data = decode_cursor(cursor)
        return KeysetCursor(sort_value=data["v"], last_id=str(data["id"]))
    except (ValueError, KeyError) as exc:
        raise BadRequestError(
            "El cursor de paginación no es válido; vuelve a empezar la lista.",
            errors=[{"pointer": "/cursor", "detail": "cursor ilegible"}],
        ) from exc


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    """Una página de resultados más el cursor de la siguiente."""

    items: list[T]
    next_cursor: str | None


def as_cursor_value(value: Any) -> Any:
    """Normaliza el valor de ordenación para que sobreviva al viaje por JSON."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value
