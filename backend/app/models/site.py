"""``observing_sites`` — sitios con nombre y reutilizables."""

from __future__ import annotations

import uuid
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import Boolean, Float, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

__all__ = ["ObservingSite"]


class ObservingSite(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "observing_sites"

    #: NULL = sitio público del catálogo ("Observatorio del Teide").
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
    )
    elevation_m: Mapped[float | None] = mapped_column(Float)
    bortle: Mapped[int | None] = mapped_column(SmallInteger)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    __table_args__ = (
        Index("ix_observing_sites_location_gist", "location", postgresql_using="gist"),
        Index("ix_observing_sites_owner", "owner_id"),
    )
