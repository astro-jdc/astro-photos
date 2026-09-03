"""Acceso a datos de ``users``."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.models.user import User

__all__ = ["UserRepository"]


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_sub(self, cognito_sub: str) -> User | None:
        stmt = select(User).where(User.cognito_sub == cognito_sub)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user

    async def public_photo_count(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            Photo.owner_id == user_id,
            Photo.deleted_at.is_(None),
            Photo.status == PhotoStatus.READY,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def reserve_quota(self, user_id: uuid.UUID, delta_bytes: int) -> None:
        """Suma bytes a ``storage_used_bytes``.

        La comprobación de cuota vive en el servicio (necesita devolver un 413 con
        detalle); aquí solo se aplica el incremento de forma atómica.
        """
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(storage_used_bytes=User.storage_used_bytes + delta_bytes)
            # Igual que en los contadores de `photo.py`: sin esto SQLAlchemy
            # expira el `User` que trae la petición y el siguiente acceso a un
            # atributo hace IO perezosa fuera del greenlet (`MissingGreenlet`).
            .execution_options(synchronize_session=False)
        )
