"""Validación de JWT en modo ``local`` y derivación de roles.

El modo ``cognito`` (JWKS remoto) no se prueba aquí: necesita red y se cubre en el
entorno de staging. Lo que sí se prueba es todo lo que decide **quién eres y qué
puedes hacer**, que es donde un fallo silencioso es peor.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import (
    AuthenticatedUser,
    Role,
    create_local_token,
    decode_token,
    require_role,
)


@pytest.fixture
def local_settings() -> Settings:
    return Settings(
        auth_mode="local",
        jwt_secret="secreto-de-test-suficientemente-largo-32b",
        environment="test",
    )


# --------------------------------------------------------------------------- #
# Emisión y validación
# --------------------------------------------------------------------------- #
def test_a_locally_issued_token_round_trips(local_settings: Settings) -> None:
    user_id = uuid.uuid4()
    token = create_local_token(
        sub="sub-1", user_id=user_id, email="a@b.org", settings=local_settings
    )
    identity = decode_token(token, local_settings)
    assert identity.id == user_id
    assert identity.sub == "sub-1"
    assert identity.email == "a@b.org"
    assert identity.role is Role.MEMBER


def test_an_expired_token_is_rejected(local_settings: Settings) -> None:
    token = create_local_token(sub="s", expires_in=-1, settings=local_settings)
    with pytest.raises(UnauthorizedError, match="caducado"):
        decode_token(token, local_settings)


def test_a_token_signed_with_another_secret_is_rejected(local_settings: Settings) -> None:
    other = Settings(
        auth_mode="local",
        jwt_secret="otro-secreto-igualmente-largo-de-32-bytes",
        environment="test",
    )
    token = create_local_token(sub="s", settings=other)
    with pytest.raises(UnauthorizedError, match="no es válido"):
        decode_token(token, local_settings)


def test_garbage_is_rejected_without_leaking_internals(local_settings: Settings) -> None:
    with pytest.raises(UnauthorizedError) as exc:
        decode_token("no-soy-un-jwt", local_settings)
    assert "no es válido" in exc.value.detail
    assert "Signature" not in exc.value.detail


def test_a_token_without_sub_is_rejected(local_settings: Settings) -> None:
    raw = jwt.encode({"exp": int(time.time()) + 60}, local_settings.jwt_secret, algorithm="HS256")
    with pytest.raises(UnauthorizedError, match="sub"):
        decode_token(raw, local_settings)


def test_an_unparsable_user_id_claim_does_not_crash(local_settings: Settings) -> None:
    """Un claim corrupto degrada a «sin id», no tumba la petición."""
    raw = jwt.encode(
        {"sub": "s", "exp": int(time.time()) + 60, "custom:user_id": "no-uuid"},
        local_settings.jwt_secret,
        algorithm="HS256",
    )
    identity = decode_token(raw, local_settings)
    assert identity.id is None
    assert identity.sub == "s"


def test_local_token_issuance_is_refused_in_cognito_mode() -> None:
    cognito = Settings(
        auth_mode="cognito",
        cognito_user_pool_id="eu-west-1_abc",
        cognito_client_id="cid",
        environment="test",
    )
    with pytest.raises(RuntimeError, match="AUTH_MODE=local"):
        create_local_token(sub="s", settings=cognito)


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("role", "rank"), [(Role.MEMBER, 0), (Role.CURATOR, 1), (Role.ADMIN, 2)])
def test_role_ranking_is_ordered(role: Role, rank: int) -> None:
    assert role.rank == rank


@pytest.mark.parametrize("role", list(Role))
def test_role_travels_in_the_token(role: Role, local_settings: Settings) -> None:
    token = create_local_token(sub="s", role=role, settings=local_settings)
    assert decode_token(token, local_settings).role is role


def test_the_highest_cognito_group_wins(local_settings: Settings) -> None:
    raw = jwt.encode(
        {
            "sub": "s",
            "exp": int(time.time()) + 60,
            "cognito:groups": ["member", "admin", "curator"],
        },
        local_settings.jwt_secret,
        algorithm="HS256",
    )
    assert decode_token(raw, local_settings).role is Role.ADMIN


def test_an_unknown_group_falls_back_to_member(local_settings: Settings) -> None:
    raw = jwt.encode(
        {"sub": "s", "exp": int(time.time()) + 60, "cognito:groups": ["superadmin"]},
        local_settings.jwt_secret,
        algorithm="HS256",
    )
    assert decode_token(raw, local_settings).role is Role.MEMBER


def test_an_unknown_custom_role_falls_back_to_member(local_settings: Settings) -> None:
    raw = jwt.encode(
        {"sub": "s", "exp": int(time.time()) + 60, "custom:role": "dios"},
        local_settings.jwt_secret,
        algorithm="HS256",
    )
    assert decode_token(raw, local_settings).role is Role.MEMBER


@pytest.mark.parametrize(
    ("has", "needs", "allowed"),
    [
        (Role.MEMBER, Role.MEMBER, True),
        (Role.MEMBER, Role.CURATOR, False),
        (Role.MEMBER, Role.ADMIN, False),
        (Role.CURATOR, Role.MEMBER, True),
        (Role.CURATOR, Role.ADMIN, False),
        (Role.ADMIN, Role.ADMIN, True),
        (Role.ADMIN, Role.MEMBER, True),
    ],
)
def test_has_role_covers_the_whole_matrix(has: Role, needs: Role, allowed: bool) -> None:
    user = AuthenticatedUser(id=None, sub="s", email=None, role=has, claims={})
    assert user.has_role(needs) is allowed


async def test_require_role_raises_403_when_the_rank_is_too_low() -> None:
    dependency = require_role(Role.ADMIN)
    member = AuthenticatedUser(id=None, sub="s", email=None, role=Role.MEMBER, claims={})
    with pytest.raises(ForbiddenError, match="admin"):
        await dependency(member)


async def test_require_role_passes_the_user_through_when_allowed() -> None:
    dependency = require_role(Role.CURATOR)
    admin = AuthenticatedUser(id=None, sub="s", email=None, role=Role.ADMIN, claims={})
    assert await dependency(admin) is admin


def test_a_short_secret_is_refused_in_production() -> None:
    """Un HS256 con secreto corto es forzable; en producción no arranca."""
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(auth_mode="local", environment="prod", jwt_secret="corto")


def test_the_example_secret_is_refused_in_production() -> None:
    with pytest.raises(ValueError, match="valor de ejemplo"):
        Settings(
            auth_mode="local",
            environment="prod",
            jwt_secret="cambia-esto-en-local",
        )


def test_a_short_secret_is_tolerated_in_development() -> None:
    """En local el placeholder de `.env.example` tiene que seguir funcionando."""
    assert Settings(auth_mode="local", environment="dev", jwt_secret="corto").jwt_secret


# --------------------------------------------------------------------------- #
# Configuración de Cognito
# --------------------------------------------------------------------------- #
def test_cognito_issuer_and_jwks_urls_are_derived_from_the_pool() -> None:
    cfg = Settings(
        auth_mode="cognito",
        cognito_user_pool_id="eu-west-1_ABC123",
        cognito_region="eu-west-1",
        environment="test",
    )
    assert cfg.cognito_issuer == ("https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_ABC123")
    assert cfg.cognito_jwks_url.endswith("/.well-known/jwks.json")


def test_cognito_mode_without_a_pool_id_fails_loudly() -> None:
    cfg = Settings(auth_mode="cognito", cognito_user_pool_id=None, environment="test")
    with pytest.raises(ValueError, match="COGNITO_USER_POOL_ID"):
        _ = cfg.cognito_issuer
