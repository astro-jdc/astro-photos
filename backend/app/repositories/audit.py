"""``audit_log`` — escritura append-only."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

__all__ = ["AuditRepository", "hash_ip"]


def hash_ip(ip: str | None, salt: str) -> str | None:
    """SHA-256 de la IP con sal por entorno. Nunca se guarda la IP en claro."""
    if not ip:
        return None
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        ip_hash: str | None = None,
    ) -> None:
        """Escribe una entrada. Nunca se actualiza ni se borra una fila existente."""
        self.session.add(
            AuditLog(
                actor_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload,
                ip_hash=ip_hash,
            )
        )
