"""Schemas de ``/me`` y ``/users/{id}``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.security import Role
from app.domain.licensing import LicenseCode
from app.schemas.common import Schema

__all__ = ["MeOut", "PublicUserOut", "StorageQuotaOut", "UserUpdateIn"]


class StorageQuotaOut(Schema):
    quota_bytes: int
    used_bytes: int
    available_bytes: int
    used_fraction: float = Field(ge=0.0)


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
    storage: StorageQuotaOut
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
