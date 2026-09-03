"""``/me`` y ``/users/{id}``."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentDbUser, DbSession
from app.core.errors import NotFoundError
from app.core.security import Role
from app.models.user import User
from app.repositories.user import UserRepository
from app.schemas.user import MeOut, PublicUserOut, StorageQuotaOut, UserUpdateIn

router = APIRouter(tags=["auth"])


def _me(user: User) -> MeOut:
    quota = user.storage_quota_bytes or 1
    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        bio=user.bio,
        website_url=user.website_url,
        attribution_name=user.attribution_name,
        default_license=user.default_license,
        role=Role(user.role),
        is_active=user.is_active,
        storage=StorageQuotaOut(
            quota_bytes=user.storage_quota_bytes,
            used_bytes=user.storage_used_bytes,
            available_bytes=user.storage_available_bytes,
            used_fraction=min(1.0, user.storage_used_bytes / quota),
        ),
        created_at=user.created_at,
    )


@router.get("/me", response_model=MeOut, summary="Perfil del usuario actual + cuota")
async def read_me(user: CurrentDbUser) -> MeOut:
    return _me(user)


@router.patch("/me", response_model=MeOut, summary="Edita el perfil")
async def update_me(user: CurrentDbUser, payload: UserUpdateIn) -> MeOut:
    for field in (
        "display_name",
        "bio",
        "website_url",
        "default_license",
        "attribution_name",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(user, field, value)
    return _me(user)


@router.get(
    "/users/{user_id}",
    response_model=PublicUserOut,
    summary="Perfil público de un usuario",
)
async def read_public_user(user_id: UUID, session: DbSession) -> PublicUserOut:
    users = UserRepository(session)
    user = await users.get(user_id)
    if user is None or not user.is_active:
        raise NotFoundError("El usuario no existe.")
    return PublicUserOut(
        id=user.id,
        display_name=user.display_name,
        bio=user.bio,
        website_url=user.website_url,
        attribution_name=user.attribution_name,
        photo_count=await users.public_photo_count(user.id),
        created_at=user.created_at,
    )
