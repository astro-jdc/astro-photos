"""Smoke de la API con ``TestClient`` y las dependencias mockeadas.

Verifica tres cosas que no necesitan base de datos:

1. que **todas** las rutas de ``docs/api.md`` existen y con el método correcto,
2. que los errores salen en ``application/problem+json`` (RFC 9457),
3. que el OpenAPI se genera (el frontend genera sus tipos de ahí).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.core.errors import PROBLEM_CONTENT_TYPE
from app.core.security import current_user, optional_user
from app.db.session import get_session
from app.main import create_app

#: Las rutas del contrato, con el método y si necesitan token.
CONTRACT_ROUTES: list[tuple[str, str]] = [
    ("GET", "/me"),
    ("PATCH", "/me"),
    ("GET", "/users/{user_id}"),
    ("POST", "/photos/uploads"),
    ("POST", "/photos/{photo_id}/complete"),
    ("GET", "/photos"),
    ("GET", "/photos/{photo_id}"),
    ("PATCH", "/photos/{photo_id}"),
    ("DELETE", "/photos/{photo_id}"),
    ("GET", "/photos/{photo_id}/download"),
    ("GET", "/photos/similar/{photo_id}"),
    ("GET", "/objects"),
    ("GET", "/objects/{object_id}"),
    ("GET", "/objects/{object_id}/coverage"),
    ("POST", "/reconstructions/preview"),
    ("POST", "/reconstructions"),
    ("GET", "/reconstructions"),
    ("GET", "/reconstructions/{reconstruction_id}"),
    ("GET", "/reconstructions/{reconstruction_id}/events"),
    ("GET", "/reconstructions/{reconstruction_id}/inputs"),
    ("GET", "/reconstructions/{reconstruction_id}/result"),
    ("DELETE", "/reconstructions/{reconstruction_id}"),
    ("GET", "/models"),
    ("GET", "/models/{model_id}"),
    ("POST", "/models/{model_id}/activate"),
    ("GET", "/licenses"),
    ("POST", "/licenses/resolve"),
    ("GET", "/healthz"),
    ("GET", "/readyz"),
]


class _FakeSession:
    """Sesión que no habla con nada. Cualquier consulta es un error explícito."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Este test no debe tocar la base de datos.")

    async def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture
def app(fake_user: Any) -> Iterator[FastAPI]:
    application = create_app()

    async def _session() -> Any:
        return _FakeSession()

    async def _identity() -> Any:
        from app.core.security import AuthenticatedUser, Role

        return AuthenticatedUser(
            id=fake_user.id, sub="test-sub", email=fake_user.email, role=Role.MEMBER, claims={}
        )

    async def _db_user() -> Any:
        return fake_user

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[current_user] = _identity
    application.dependency_overrides[optional_user] = _identity
    application.dependency_overrides[deps.get_db_user] = _db_user
    application.dependency_overrides[deps.get_optional_db_user] = _db_user
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #
def test_every_contract_route_exists(client: TestClient) -> None:
    """Ninguna ruta de ``docs/api.md`` puede faltar.

    Se comprueba sobre el OpenAPI y no sobre ``app.routes`` porque el OpenAPI **es**
    el contrato: es de donde el frontend genera sus tipos.
    """
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    registered = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, operations in paths.items()
        for method in operations
    }
    missing = [entry for entry in CONTRACT_ROUTES if entry not in registered]
    assert missing == [], f"Rutas del contrato que faltan: {missing}"


def test_openapi_is_generated_and_is_31(client: TestClient) -> None:
    """El frontend genera sus tipos de aquí (regla dura 7)."""
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.1")
    assert schema["info"]["title"] == "astro-photos API"
    assert len(schema["paths"]) >= 20


def test_healthz_is_alive(client: TestClient) -> None:
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_request_id_is_echoed_back(client: TestClient) -> None:
    response = client.get("/api/v1/healthz", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


# --------------------------------------------------------------------------- #
# Licencias: la única ruta que funciona sin base de datos
# --------------------------------------------------------------------------- #
def test_license_catalog_lists_the_eight_licenses(client: TestClient) -> None:
    response = client.get("/api/v1/licenses")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 8
    assert body["default_license"] == "CC-BY-NC-4.0"
    assert sum(1 for item in body["items"] if item["is_default"]) == 1


def test_license_catalog_exposes_the_flags_the_frontend_needs(client: TestClient) -> None:
    items = {i["code"]: i for i in client.get("/api/v1/licenses").json()["items"]}
    assert items["CC-BY-NC-ND-4.0"]["allows_derivatives"] is False
    assert items["CC0-1.0"]["restrictiveness"] == 0
    assert items["ARR"]["restrictiveness"] == 7


# --------------------------------------------------------------------------- #
# RFC 9457
# --------------------------------------------------------------------------- #
def test_unknown_route_returns_problem_json(client: TestClient) -> None:
    response = client.get("/api/v1/no-existe")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail", "instance"}
    assert body["status"] == 404
    assert body["instance"] == "/api/v1/no-existe"


def test_validation_error_returns_problem_json_with_field_pointers(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/licenses/resolve", json={"photo_ids": "no-es-lista"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert body["type"].endswith("/invalid-request")
    assert body["errors"], "un 422 de validación debe decir qué campo falla"
    assert all("pointer" in e and "detail" in e for e in body["errors"])


def test_empty_photo_id_list_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/licenses/resolve", json={"photo_ids": []})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    """Los schemas son ``extra='forbid'``: un typo del cliente no pasa en silencio."""
    response = client.patch("/api/v1/me", json={"display_nmae": "typo"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


def test_bad_uuid_in_the_path_returns_problem_json(client: TestClient) -> None:
    response = client.get("/api/v1/objects/no-soy-un-uuid")
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


def test_missing_token_returns_401_problem_json() -> None:
    """Sin overrides, una ruta protegida contesta 401 con ``WWW-Authenticate``."""
    application = create_app()
    with TestClient(application, raise_server_exceptions=False) as bare:
        response = bare.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert "Bearer" in response.headers.get("WWW-Authenticate", "")
    assert response.json()["type"].endswith("/unauthorized")


def test_invalid_token_returns_401_without_leaking_details() -> None:
    application = create_app()
    with TestClient(application, raise_server_exceptions=False) as bare:
        response = bare.get("/api/v1/me", headers={"Authorization": "Bearer basura"})
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "Signature" not in detail and "padding" not in detail


def test_an_unexpected_exception_never_leaks_its_message(app: FastAPI) -> None:
    """Regla dura 4: nunca un 500 desnudo ni un mensaje de excepción crudo."""

    @app.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("secreto interno: contraseña=hunter2")

    with TestClient(app, raise_server_exceptions=False) as broken:
        response = broken.get("/api/v1/_boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert "hunter2" not in response.text
    assert body["title"] == "Error interno"
    assert "X-Request-ID" in body["detail"]


# --------------------------------------------------------------------------- #
# Privacidad de la ubicación en el serializador
# --------------------------------------------------------------------------- #
def _photo(precision: str, owner_id: uuid.UUID) -> Any:
    from app.domain.licensing import LicenseCode
    from app.domain.location import LocationPrecision
    from app.models.enums import PhotoStatus
    from app.models.photo import Photo

    photo = Photo(
        id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        owner_id=owner_id,
        status=PhotoStatus.READY,
        s3_bucket="bucket",
        s3_key_original="key",
        checksum_sha256=b"\x00" * 32,
        license=LicenseCode.CC_BY_NC,
        location_precision=LocationPrecision(precision),
        location_accuracy_m=10.0,
        elevation_m=2390.0,
        country_code="ES",
    )
    photo.lat_deg = 28.3005
    photo.lon_deg = -16.5117
    photo.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    photo.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return photo


@pytest.mark.parametrize(
    ("precision", "expect_lat"),
    [("exact", 28.3005), ("city", 28.3), ("country", 40.24), ("hidden", None)],
)
def test_serializer_obfuscates_location_for_third_parties(
    precision: str, expect_lat: float | None
) -> None:
    """La ofuscación se aplica en el serializador, no en la query."""
    from app.schemas.photo import PhotoOut

    photo = _photo(precision, uuid.uuid4())
    location = PhotoOut.obfuscated_location(photo, viewer_is_owner=False)
    if expect_lat is None:
        assert location is None
    else:
        assert location is not None
        assert location.lat == pytest.approx(expect_lat)


@pytest.mark.parametrize("precision", ["exact", "city", "country", "hidden"])
def test_the_owner_always_sees_the_exact_location(precision: str, user_id: uuid.UUID) -> None:
    """La privacidad protege de terceros, no del propio autor."""
    from app.schemas.photo import PhotoOut

    photo = _photo(precision, user_id)
    location = PhotoOut.obfuscated_location(photo, viewer_is_owner=True)
    assert location is not None
    assert location.lat == pytest.approx(28.3005)
    assert location.lon == pytest.approx(-16.5117)


def test_a_photo_without_coordinates_publishes_no_location() -> None:
    from app.schemas.photo import PhotoOut

    photo = _photo("exact", uuid.uuid4())
    photo.lat_deg = None
    photo.lon_deg = None
    assert PhotoOut.obfuscated_location(photo, viewer_is_owner=False) is None
