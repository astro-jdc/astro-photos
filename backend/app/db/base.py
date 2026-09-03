"""Base declarativa y mixins comunes."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["Base", "TimestampMixin", "UUIDPkMixin", "metadata"]

#: Convención de nombres para que Alembic genere migraciones estables y para poder
#: hacer `DROP CONSTRAINT` por nombre en un rollback.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Base de todos los modelos."""

    metadata = metadata


class UUIDPkMixin:
    """``id UUID PRIMARY KEY DEFAULT gen_random_uuid()`` (``docs/data-model.md``)."""

    id: Mapped[uuid.UUID] = mapped_column(
        pg.UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at TIMESTAMPTZ NOT NULL DEFAULT now()``."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
