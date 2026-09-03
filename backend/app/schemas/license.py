"""Schemas de ``/licenses`` 🔓 — catálogo y resolución."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.domain.licensing import (
    LICENSE_CATALOG,
    BlockedPhoto,
    BlockReason,
    LicenseCode,
    LicenseInfo,
    LicenseResolution,
)
from app.schemas.common import Schema

__all__ = [
    "BlockedPhotoOut",
    "LicenseCatalogOut",
    "LicenseInfoOut",
    "LicenseResolveIn",
    "LicenseResolveOut",
]


class LicenseInfoOut(Schema):
    """Una fila del catálogo con sus flags."""

    code: LicenseCode
    name: str
    name_es: str
    version: str
    url: str
    allows_commercial: bool
    allows_derivatives: bool
    requires_attribution: bool
    requires_sharealike: bool
    restrictiveness: int
    spdx_id: str | None = None
    #: ``true`` en la que viene preseleccionada en el formulario de subida.
    is_default: bool = False

    @classmethod
    def from_domain(cls, info: LicenseInfo, *, is_default: bool = False) -> LicenseInfoOut:
        return cls(
            code=info.code,
            name=info.name,
            name_es=info.name_es,
            version=info.version,
            url=info.url,
            allows_commercial=info.allows_commercial,
            allows_derivatives=info.allows_derivatives,
            requires_attribution=info.requires_attribution,
            requires_sharealike=info.requires_sharealike,
            restrictiveness=info.restrictiveness,
            spdx_id=info.spdx_id,
            is_default=is_default,
        )


class LicenseCatalogOut(Schema):
    """``GET /licenses``."""

    items: list[LicenseInfoOut]
    default_license: LicenseCode

    @classmethod
    def build(cls, default: LicenseCode) -> LicenseCatalogOut:
        return cls(
            items=[
                LicenseInfoOut.from_domain(i, is_default=i.code == default) for i in LICENSE_CATALOG
            ],
            default_license=default,
        )


class LicenseResolveIn(Schema):
    """``POST /licenses/resolve``."""

    photo_ids: list[UUID] = Field(min_length=1, max_length=1000)


class BlockedPhotoOut(Schema):
    photo_id: UUID
    reason: BlockReason
    detail: str
    license: LicenseCode

    @classmethod
    def from_domain(cls, blocked: BlockedPhoto) -> BlockedPhotoOut:
        return cls(
            photo_id=UUID(blocked.photo_id),
            reason=blocked.reason,
            detail=blocked.detail,
            license=blocked.license,
        )


class LicenseResolveOut(Schema):
    """``{resulting_license, blocked: [{photo_id, reason}]}`` (``docs/api.md``)."""

    resulting_license: LicenseCode | None
    blocked: list[BlockedPhotoOut] = Field(default_factory=list)
    requires_attribution: bool = True
    notes: list[str] = Field(default_factory=list)
    #: Fotos pedidas que no existen o no son visibles para quien pregunta.
    unknown_photo_ids: list[UUID] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls,
        resolution: LicenseResolution,
        *,
        unknown: list[UUID] | None = None,
    ) -> LicenseResolveOut:
        return cls(
            resulting_license=resolution.resulting_license,
            blocked=[BlockedPhotoOut.from_domain(b) for b in resolution.blocked],
            requires_attribution=resolution.requires_attribution,
            notes=list(resolution.notes),
            unknown_photo_ids=unknown or [],
        )
