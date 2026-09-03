"""``users`` — cuentas y cuota."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Index, String, Text, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import Role
from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.domain.licensing import LicenseCode
from app.models.enums import license_code_enum, user_role_enum

if TYPE_CHECKING:
    from app.models.collection import Collection
    from app.models.photo import Photo

__all__ = ["User"]


class User(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    #: Sujeto de Cognito. NULL en cuentas de sistema (workers, seeds).
    cognito_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    #: Se muestra como autoría en la licencia CC; por eso es NOT NULL.
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)
    default_license: Mapped[LicenseCode] = mapped_column(
        license_code_enum,
        nullable=False,
        server_default=text(f"'{LicenseCode.CC_BY_NC.value}'::license_code"),
        default=LicenseCode.CC_BY_NC,
    )
    role: Mapped[Role] = mapped_column(
        user_role_enum,
        nullable=False,
        server_default=text("'member'::user_role"),
        default=Role.MEMBER,
    )
    storage_quota_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("21474836480"), default=21474836480
    )
    #: Mantenido por trigger sobre ``photos`` (ver la migración inicial).
    storage_used_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    #: Nombre por defecto para la atribución si el autor no lo fija por foto.
    attribution_name: Mapped[str | None] = mapped_column(String(200))

    photos: Mapped[list[Photo]] = relationship(
        back_populates="owner", lazy="raise", passive_deletes=True
    )
    collections: Mapped[list[Collection]] = relationship(
        back_populates="owner", lazy="raise", passive_deletes=True
    )

    __table_args__ = (Index("ix_users_cognito_sub", "cognito_sub"),)

    @property
    def storage_available_bytes(self) -> int:
        return max(0, self.storage_quota_bytes - self.storage_used_bytes)

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"<User {self.id} {self.email}>"
