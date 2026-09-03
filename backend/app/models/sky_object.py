"""``sky_objects`` — catálogo canónico (Messier, NGC/IC, Caldwell, planetas, cometas)."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    ObjectCatalog,
    ObjectType,
    object_catalog_enum,
    object_type_enum,
)

__all__ = ["SkyObject"]


class SkyObject(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "sky_objects"

    catalog: Mapped[ObjectCatalog] = mapped_column(object_catalog_enum, nullable=False)
    catalog_number: Mapped[str] = mapped_column(Text, nullable=False)
    common_name: Mapped[str | None] = mapped_column(Text)
    common_name_es: Mapped[str | None] = mapped_column(Text)
    object_type: Mapped[ObjectType] = mapped_column(
        object_type_enum,
        nullable=False,
        server_default=text("'other'::object_type"),
        default=ObjectType.OTHER,
    )
    #: J2000. NULL en objetos móviles (``is_ephemeral``).
    ra_deg: Mapped[float | None] = mapped_column(Float(precision=53))
    dec_deg: Mapped[float | None] = mapped_column(Float(precision=53))
    magnitude: Mapped[float | None] = mapped_column(Float)
    size_arcmin: Mapped[float | None] = mapped_column(Float)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"), default=list
    )
    #: Planetas y cometas: sin RA/Dec fijas, se resuelven por efemérides.
    is_ephemeral: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    #: Denormalizados para el listado; los refresca un job periódico.
    photo_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    reconstruction_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    __table_args__ = (
        UniqueConstraint("catalog", "catalog_number", name="uq_sky_objects_catalog_number"),
        Index("ix_sky_objects_radec", "ra_deg", "dec_deg"),
        Index("ix_sky_objects_aliases_gin", "aliases", postgresql_using="gin"),
        Index("ix_sky_objects_common_name", "common_name"),
    )

    @property
    def designation(self) -> str:
        """``M31``, ``NGC 224``… tal como lo escribe la gente."""
        sep = "" if self.catalog is ObjectCatalog.MESSIER else " "
        return f"{self.catalog.value}{sep}{self.catalog_number}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SkyObject {self.designation}>"
