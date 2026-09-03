"""``collections`` y ``collection_photos`` — álbumes de usuario, N:M con posición."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin

if TYPE_CHECKING:
    from app.models.user import User

__all__ = ["Collection", "CollectionPhoto"]


class Collection(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "collections"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )

    owner: Mapped[User] = relationship(back_populates="collections", lazy="raise")
    items: Mapped[list[CollectionPhoto]] = relationship(
        back_populates="collection", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_collections_owner", "owner_id"),)


class CollectionPhoto(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "collection_photos"

    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    photo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    collection: Mapped[Collection] = relationship(back_populates="items", lazy="raise")

    __table_args__ = (
        UniqueConstraint("collection_id", "photo_id", name="uq_collection_photos_pair"),
        Index("ix_collection_photos_collection_position", "collection_id", "position"),
    )
