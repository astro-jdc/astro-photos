"""``licenses`` — tabla de referencia con semilla fija.

Es un espejo persistido de ``app.domain.licensing.LICENSE_CATALOG``. La lógica de
combinación **no** lee de aquí: la fuente de verdad sigue siendo el módulo de
dominio. Esta tabla existe para que los `JOIN` y los informes SQL no tengan que
duplicar los flags, y la migración inicial la siembra desde el catálogo del dominio.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.domain.licensing import LicenseCode
from app.models.enums import license_code_enum

__all__ = ["License"]


class License(TimestampMixin, Base):
    __tablename__ = "licenses"

    code: Mapped[LicenseCode] = mapped_column(license_code_enum, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_es: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    allows_commercial: Mapped[bool] = mapped_column(Boolean, nullable=False)
    allows_derivatives: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_attribution: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_sharealike: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Entero para el cálculo de compatibilidad; ver ``docs/licensing.md``.
    restrictiveness: Mapped[int] = mapped_column(Integer, nullable=False)
    spdx_id: Mapped[str | None] = mapped_column(Text)
