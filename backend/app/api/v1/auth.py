"""``/me`` y ``/users/{id}``."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentDbUser, DbSession, SettingsDep
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.security import Role
from app.models.user import User
from app.repositories.reconstruction import ReconstructionRepository
from app.repositories.user import UserRepository
from app.schemas.user import MeOut, PublicUserOut, QuotaOut, UserUpdateIn

router = APIRouter(tags=["auth"])


async def _me(user: User, session: DbSession, settings: Settings) -> MeOut:
    """Perfil + cuota, incluidos los límites de trabajos vigentes.

    Los contadores de trabajos viajan aquí (``docs/api.md``) para que el cliente
    pueda deshabilitar el botón de reconstruir en vez de comerse un 429.
    """
    jobs = ReconstructionRepository(session)
    quota_bytes = user.storage_quota_bytes or 1
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
        quota=QuotaOut(
            quota_bytes=user.storage_quota_bytes,
            used_bytes=user.storage_used_bytes,
            available_bytes=user.storage_available_bytes,
            used_fraction=min(1.0, user.storage_used_bytes / quota_bytes),
            max_queued_jobs=settings.max_queued_jobs_per_user,
            max_jobs_per_day=settings.max_jobs_per_day,
            jobs_queued_now=await jobs.count_active_for_user(user.id),
            jobs_today=await jobs.count_last_24h(user.id),
        ),
        created_at=user.created_at,
    )


@router.get("/me", response_model=MeOut, summary="Perfil del usuario actual + cuota")
async def read_me(user: CurrentDbUser, session: DbSession, settings: SettingsDep) -> MeOut:
    return await _me(user, session, settings)


@router.patch("/me", response_model=MeOut, summary="Edita el perfil")
async def update_me(
    user: CurrentDbUser,
    payload: UserUpdateIn,
    session: DbSession,
    settings: SettingsDep,
) -> MeOut:
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
    return await _me(user, session, settings)


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
