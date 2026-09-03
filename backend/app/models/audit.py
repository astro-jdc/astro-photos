"""``audit_log`` — append-only.

Toda mutación de licencia, borrado y descarga masiva queda aquí
(``docs/data-model.md``). La IP se guarda **hasheada**: sirve para detectar abuso,
no para identificar a nadie.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

__all__ = ["AuditLog"]


class AuditLog(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "audit_log"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(pg.UUID(as_uuid=True))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: SHA-256 de la IP con sal por entorno; nunca la IP en claro.
    ip_hash: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_log_actor", "actor_id", "created_at"),
        Index("ix_audit_log_action", "action", "created_at"),
    )
