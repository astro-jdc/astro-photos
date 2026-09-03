"""Dependencias compartidas de la API: sesión, servicios, usuario de base de datos."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, NotFoundError
from app.core.security import AuthenticatedUser, Role, current_user, optional_user
from app.db.session import get_session
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.ml import ModelRepository
from app.repositories.photo import PhotoRepository
from app.repositories.reconstruction import ReconstructionRepository
from app.repositories.sky_object import SkyObjectRepository
from app.repositories.user import UserRepository
from app.services.photo import PhotoService
from app.services.queue import QueueService
from app.services.reconstruction import ReconstructionService
from app.services.sky_object import ObjectService
from app.services.storage import StorageService
from app.services.upload import UploadService

__all__ = [
    "CurrentDbUser",
    "DbSession",
    "IdempotencyKey",
    "OptionalDbUser",
    "SettingsDep",
    "get_object_service",
    "get_photo_service",
    "get_reconstruction_service",
    "get_upload_service",
]

DbSession = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: Cabecera de idempotencia de los POST que crean trabajo (regla dura 3).
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description=(
            "Clave de idempotencia. Repetir el POST con la misma clave devuelve el "
            "trabajo ya creado en vez de crear otro."
        ),
        max_length=200,
    ),
]


# --------------------------------------------------------------------------- #
# Servicios de infraestructura: uno por proceso, no por petición.
# --------------------------------------------------------------------------- #
_storage: StorageService | None = None
_queue: QueueService | None = None


def get_storage(settings: SettingsDep) -> StorageService:
    global _storage
    if _storage is None:
        _storage = StorageService(settings)
    return _storage


def get_queue(settings: SettingsDep) -> QueueService:
    global _queue
    if _queue is None:
        _queue = QueueService(settings)
    return _queue


def reset_infrastructure() -> None:
    """Limpia los singletons. Solo para tests."""
    global _storage, _queue
    _storage = None
    _queue = None


# --------------------------------------------------------------------------- #
# Usuario de base de datos
# --------------------------------------------------------------------------- #
async def get_db_user(
    session: DbSession,
    identity: Annotated[AuthenticatedUser, Depends(current_user)],
) -> User:
    """Traduce la identidad del token a la fila de ``users``.

    Si el token trae un ``sub`` que no está en la base, es un 403 y no un 404: el
    token es válido pero la cuenta no existe aquí todavía (o fue desactivada).
    """
    repo = UserRepository(session)
    user: User | None = None
    if identity.id is not None:
        user = await repo.get(identity.id)
    if user is None:
        user = await repo.get_by_sub(identity.sub)
    if user is None and identity.email:
        user = await repo.get_by_email(identity.email)
    if user is None:
        raise ForbiddenError(
            "El token es válido pero no hay ninguna cuenta asociada en este entorno."
        )
    if not user.is_active:
        raise ForbiddenError("La cuenta está desactivada.")
    return user


async def get_optional_db_user(
    session: DbSession,
    identity: Annotated[AuthenticatedUser | None, Depends(optional_user)],
) -> User | None:
    """Igual, pero para las rutas 🔓: devuelve ``None`` si no hay token."""
    if identity is None:
        return None
    repo = UserRepository(session)
    user: User | None = None
    if identity.id is not None:
        user = await repo.get(identity.id)
    if user is None:
        user = await repo.get_by_sub(identity.sub)
    return user if user is not None and user.is_active else None


CurrentDbUser = Annotated[User, Depends(get_db_user)]
OptionalDbUser = Annotated[User | None, Depends(get_optional_db_user)]


def require_db_role(minimum: Role) -> Any:
    """Rol mínimo comprobado contra la fila de ``users``, no solo contra el token."""

    async def _dep(user: CurrentDbUser) -> User:
        if Role(user.role).rank < minimum.rank:
            raise ForbiddenError(f"Esta operación requiere el rol «{minimum.value}» o superior.")
        return user

    return Depends(_dep)


# --------------------------------------------------------------------------- #
# Servicios de aplicación (uno por petición: llevan la sesión dentro)
# --------------------------------------------------------------------------- #
async def get_photo_service(
    session: DbSession,
    settings: SettingsDep,
    storage: Annotated[StorageService, Depends(get_storage)],
) -> PhotoService:
    return PhotoService(
        photos=PhotoRepository(session),
        reconstructions=ReconstructionRepository(session),
        audit=AuditRepository(session),
        storage=storage,
        settings=settings,
    )


async def get_upload_service(
    session: DbSession,
    settings: SettingsDep,
    storage: Annotated[StorageService, Depends(get_storage)],
    queue: Annotated[QueueService, Depends(get_queue)],
) -> UploadService:
    return UploadService(
        photos=PhotoRepository(session),
        users=UserRepository(session),
        audit=AuditRepository(session),
        storage=storage,
        queue=queue,
        settings=settings,
    )


async def get_reconstruction_service(
    session: DbSession,
    settings: SettingsDep,
    queue: Annotated[QueueService, Depends(get_queue)],
) -> ReconstructionService:
    return ReconstructionService(
        photos=PhotoRepository(session),
        reconstructions=ReconstructionRepository(session),
        objects=SkyObjectRepository(session),
        audit=AuditRepository(session),
        queue=queue,
        settings=settings,
    )


async def get_object_service(session: DbSession) -> ObjectService:
    return ObjectService(SkyObjectRepository(session))


async def get_photo_repository(session: DbSession) -> PhotoRepository:
    return PhotoRepository(session)


async def get_user_repository(session: DbSession) -> UserRepository:
    return UserRepository(session)


async def get_object_repository(session: DbSession) -> SkyObjectRepository:
    return SkyObjectRepository(session)


async def get_reconstruction_repository(session: DbSession) -> ReconstructionRepository:
    return ReconstructionRepository(session)


async def get_model_repository(session: DbSession) -> ModelRepository:
    return ModelRepository(session)


async def get_audit_repository(session: DbSession) -> AuditRepository:
    return AuditRepository(session)


async def client_ip(request: Request) -> AsyncIterator[str | None]:
    """IP del cliente respetando ``X-Forwarded-For`` del ALB."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        yield forwarded.split(",")[0].strip()
    else:
        yield request.client.host if request.client else None


async def not_found_if_none(value: object, message: str) -> None:
    if value is None:
        raise NotFoundError(message)
