"""Autenticación: validación de JWT en modo ``local`` (HS256) y ``cognito`` (JWKS).

``docs/api.md``: ``Authorization: Bearer <JWT de Cognito>``; el backend valida contra
el JWKS del User Pool. En desarrollo no hay Cognito, así que se firma localmente con
HS256 y el mismo formato de claims, para que el resto del código no note la
diferencia.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

import httpx
import jwt
import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError

__all__ = [
    "AuthenticatedUser",
    "JWKSCache",
    "Role",
    "create_local_token",
    "current_user",
    "decode_token",
    "optional_user",
    "require_role",
]

log = structlog.get_logger(__name__)


class Role(StrEnum):
    """Valores del enum de Postgres ``user_role``."""

    MEMBER = "member"
    CURATOR = "curator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {Role.MEMBER: 0, Role.CURATOR: 1, Role.ADMIN: 2}[self]


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Identidad extraída del token. No toca la base de datos.

    El ``id`` es el UUID de ``users.id``; en modo Cognito viaja en el claim
    ``custom:user_id`` y, si no está, se deriva del ``sub`` por el repositorio.
    """

    id: UUID | None
    sub: str
    email: str | None
    role: Role
    claims: dict[str, Any]

    def has_role(self, minimum: Role) -> bool:
        return self.role.rank >= minimum.rank


bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Bearer")


class JWKSCache:
    """Cachea el JWKS del User Pool. Evita un round-trip por petición.

    ``PyJWKClient`` ya cachea, pero además se le pone una vida máxima explícita para
    que una rotación de claves se recoja sin reiniciar el servicio.
    """

    def __init__(self, url: str, ttl_seconds: int) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._client: PyJWKClient | None = None
        self._fetched_at = 0.0

    def client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._client is None or now - self._fetched_at > self._ttl:
            self._client = PyJWKClient(self._url, cache_keys=True, lifespan=self._ttl)
            self._fetched_at = now
        return self._client

    async def healthy(self) -> bool:
        """Comprueba que el JWKS es alcanzable; se usa en ``/readyz``."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(self._url)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


_jwks_caches: dict[str, JWKSCache] = {}


def _jwks_cache(settings: Settings) -> JWKSCache:
    url = settings.cognito_jwks_url
    cache = _jwks_caches.get(url)
    if cache is None:
        cache = JWKSCache(url, settings.jwks_cache_seconds)
        _jwks_caches[url] = cache
    return cache


def create_local_token(
    *,
    sub: str,
    user_id: UUID | str | None = None,
    email: str | None = None,
    role: Role | str = Role.MEMBER,
    expires_in: int = 3600,
    settings: Settings | None = None,
) -> str:
    """Emite un JWT HS256 de desarrollo. **Solo** para ``AUTH_MODE=local`` y tests."""
    cfg = settings or get_settings()
    if not cfg.is_local_auth:
        raise RuntimeError("create_local_token solo está disponible con AUTH_MODE=local")
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": now,
        "exp": now + expires_in,
        "token_use": "access",
        "cognito:groups": [str(role)],
    }
    if user_id is not None:
        payload["custom:user_id"] = str(user_id)
    if email is not None:
        payload["email"] = email
    if cfg.jwt_audience:
        payload["aud"] = cfg.jwt_audience
    if cfg.jwt_issuer:
        payload["iss"] = cfg.jwt_issuer
    return jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def _role_from_claims(claims: dict[str, Any]) -> Role:
    """Rol efectivo: grupo de Cognito o claim ``custom:role``; por defecto ``member``."""
    raw = claims.get("custom:role")
    if not raw:
        groups = claims.get("cognito:groups") or []
        if isinstance(groups, list):
            known = [g for g in groups if isinstance(g, str) and g in set(Role)]
            if known:
                # Con varios grupos gana el de mayor privilegio.
                return max((Role(g) for g in known), key=lambda r: r.rank)
        return Role.MEMBER
    try:
        return Role(str(raw))
    except ValueError:
        log.warning("unknown_role_claim", value=str(raw))
        return Role.MEMBER


def decode_token(token: str, settings: Settings | None = None) -> AuthenticatedUser:
    """Valida firma, caducidad, emisor y audiencia. Lanza :class:`UnauthorizedError`."""
    cfg = settings or get_settings()
    # PyJWT declara `options` como un TypedDict; el dict literal no lo satisface.
    options = cast("Any", {"verify_aud": bool(cfg.jwt_audience or cfg.cognito_client_id)})
    try:
        if cfg.is_local_auth:
            claims: dict[str, Any] = jwt.decode(
                token,
                cfg.jwt_secret,
                algorithms=[cfg.jwt_algorithm],
                audience=cfg.jwt_audience,
                issuer=cfg.jwt_issuer,
                options=options,
            )
        else:
            signing_key = _jwks_cache(cfg).client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=cfg.cognito_client_id,
                issuer=cfg.cognito_issuer,
                options=options,
            )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("El token ha caducado.") from exc
    except jwt.InvalidTokenError as exc:
        # No se propaga el mensaje de PyJWT: puede filtrar detalles del emisor.
        log.info("invalid_token", reason=type(exc).__name__)
        raise UnauthorizedError("El token no es válido.") from exc

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise UnauthorizedError("El token no trae un `sub` utilizable.")

    raw_id = claims.get("custom:user_id")
    user_id: UUID | None = None
    if isinstance(raw_id, str):
        try:
            user_id = UUID(raw_id)
        except ValueError:
            log.warning("invalid_user_id_claim")

    email = claims.get("email")
    return AuthenticatedUser(
        id=user_id,
        sub=sub,
        email=email if isinstance(email, str) else None,
        role=_role_from_claims(claims),
        claims=claims,
    )


async def optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser | None:
    """Identidad si viene un Bearer válido, ``None`` si no viene ninguno.

    Para las rutas 🔓 de ``docs/api.md``, que son públicas pero enseñan más cosas
    (p. ej. la ubicación exacta de tus propias fotos) si te identificas.
    """
    if credentials is None or not credentials.credentials:
        return None
    user = decode_token(credentials.credentials)
    request.state.user = user
    return user


async def current_user(
    user: AuthenticatedUser | None = Depends(optional_user),
) -> AuthenticatedUser:
    """Identidad obligatoria. 401 en problem+json si falta o no es válida."""
    if user is None:
        raise UnauthorizedError("Esta operación requiere autenticación.")
    return user


def require_role(minimum: Role) -> Any:
    """Dependencia que exige un rol mínimo. Uso: ``Depends(require_role(Role.ADMIN))``."""

    async def _dependency(
        user: AuthenticatedUser = Depends(current_user),
    ) -> AuthenticatedUser:
        if not user.has_role(minimum):
            raise ForbiddenError(f"Esta operación requiere el rol «{minimum.value}» o superior.")
        return user

    return _dependency
