"""Schemas de ``/me`` y ``/users/{id}``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.security import Role
from app.domain.licensing import LicenseCode
from app.schemas.common import Schema

__all__ = ["MeOut", "PublicUserOut", "QuotaOut", "StorageQuotaOut", "UserUpdateIn"]


class QuotaOut(Schema):
    """Cuota de almacenamiento **y** límites de trabajos vigentes.

    Los límites de trabajos viajan aquí (``docs/api.md``) para que el cliente pueda
    deshabilitar el botón de reconstruir en vez de descubrir el tope con un 429.
    """

    quota_bytes: int
    used_bytes: int
    available_bytes: int
    used_fraction: float = Field(ge=0.0)
    #: Tope de trabajos simultáneos en cola o corriendo.
    max_queued_jobs: int
    #: Tope de trabajos lanzados en 24 h.
    max_jobs_per_day: int
    #: Cuántos tiene ahora mismo en cola o corriendo.
    jobs_queued_now: int
    #: Cuántos ha lanzado en las últimas 24 h.
    jobs_today: int

    @property
    def can_queue_job(self) -> bool:
        return (
            self.jobs_queued_now < self.max_queued_jobs and self.jobs_today < self.max_jobs_per_day
        )


#: Nombre anterior, mantenido como alias para no romper importaciones.
StorageQuotaOut = QuotaOut


class MeOut(Schema):
    """``GET /me`` — perfil del usuario actual + cuota."""

    id: UUID
    email: str
    display_name: str
    bio: str | None = None
    website_url: str | None = None
    attribution_name: str | None = None
    default_license: LicenseCode
    role: Role
    is_active: bool
    quota: QuotaOut
    created_at: datetime


class UserUpdateIn(Schema):
    """``PATCH /me``. Solo los cuatro campos del contrato, más el nombre de atribución."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    bio: str | None = Field(default=None, max_length=4000)
    website_url: str | None = Field(default=None, max_length=500)
    default_license: LicenseCode | None = None
    attribution_name: str | None = Field(default=None, max_length=200)


class PublicUserOut(Schema):
    """``GET /users/{id}`` 🔓 — perfil público. Sin email, sin cuota, sin rol."""

    id: UUID
    display_name: str
    bio: str | None = None
    website_url: str | None = None
    attribution_name: str | None = None
    photo_count: int = 0
    created_at: datetime
