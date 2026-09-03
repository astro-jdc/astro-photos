"""Smoke de la API con ``TestClient`` y las dependencias mockeadas.

Verifica tres cosas que no necesitan base de datos:

1. que **todas** las rutas de ``docs/api.md`` existen y con el método correcto,
2. que los errores salen en ``application/problem+json`` (RFC 9457),
3. que el OpenAPI se genera (el frontend genera sus tipos de ahí).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
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

SAMPLE_UUID = "22222222-2222-2222-2222-222222222222"
OTHER_UUID = "33333333-3333-3333-3333-333333333333"


@dataclass(frozen=True)
class ContractRoute:
    """Una fila del contrato, invocable."""

    method: str
    path: str
    ok: frozenset[int] | set[int] = field(default_factory=lambda: {200})
    body: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        """La plantilla con los parámetros de path ya sustituidos."""
        path = self.path
        for name in (
            "user_id",
            "photo_id",
            "object_id",
            "reconstruction_id",
            "model_id",
        ):
            path = path.replace("{" + name + "}", SAMPLE_UUID)
        return f"/api/v1{path}"

    def __str__(self) -> str:  # pragma: no cover - solo para el id del test
        return f"{self.method} {self.path}"


#: Cada ruta del contrato con: método, plantilla de path, cuerpo válido (si lo
#: necesita) y los códigos aceptables contra una base vacía.
#:
#: Los códigos **no** son decorativos. Comprobar que una ruta está registrada no
#: comprueba que responda: una tabla de rutas pasaría igual con todos los handlers
#: sustituidos por `raise`. Por eso cada entrada se invoca de verdad y se exige un
#: código concreto; cualquier 5xx es un fallo.
CONTRACT_ROUTES: list[ContractRoute] = [
    ContractRoute("GET", "/me", ok={200}),
    ContractRoute("PATCH", "/me", body={"bio": "hola"}, ok={200}),
    ContractRoute("GET", "/users/{user_id}", ok={404}),
    ContractRoute(
        "POST",
        "/photos/uploads",
        body={
            "filename": "m31.jpg",
            "size_bytes": 1024,
            "mime_type": "image/jpeg",
            "checksum_sha256": "a" * 64,
        },
        ok={201, 502},
    ),
    ContractRoute(
        "POST",
        "/photos/{photo_id}/complete",
        body={"location_precision": "hidden"},
        ok={404},
    ),
    ContractRoute(
        "POST",
        "/photos/{photo_id}/uploads/complete-multipart",
        body={"upload_id": "u", "parts": [{"part_number": 1, "etag": "e"}]},
        ok={404},
    ),
    ContractRoute("DELETE", "/photos/{photo_id}/uploads", ok={404}),
    ContractRoute("GET", "/photos", ok={200}),
    ContractRoute("GET", "/photos/{photo_id}", ok={404}),
    ContractRoute("PATCH", "/photos/{photo_id}", body={"title": "x"}, ok={404}),
    ContractRoute("DELETE", "/photos/{photo_id}", ok={404}),
    ContractRoute("GET", "/photos/{photo_id}/download", ok={404}),
    ContractRoute("GET", "/photos/similar/{photo_id}", ok={404}),
    ContractRoute("GET", "/objects", ok={200}),
    ContractRoute("GET", "/objects/{object_id}", ok={404}),
    ContractRoute("GET", "/objects/{object_id}/coverage", ok={404}),
    ContractRoute(
        "POST",
        "/reconstructions/preview",
        body={"photo_ids": [SAMPLE_UUID, OTHER_UUID], "pipeline": "classical-stack-v1"},
        ok={422},
    ),
    ContractRoute(
        "POST",
        "/reconstructions",
        body={"photo_ids": [SAMPLE_UUID, OTHER_UUID], "pipeline": "classical-stack-v1"},
        ok={422},
    ),
    ContractRoute("GET", "/reconstructions", ok={200}),
    ContractRoute("GET", "/reconstructions/{reconstruction_id}", ok={404}),
    ContractRoute("GET", "/reconstructions/{reconstruction_id}/events", ok={404}),
    ContractRoute("GET", "/reconstructions/{reconstruction_id}/inputs", ok={404}),
    ContractRoute("GET", "/reconstructions/{reconstruction_id}/result", ok={404}),
    ContractRoute("DELETE", "/reconstructions/{reconstruction_id}", ok={404}),
    ContractRoute("GET", "/models", ok={200}),
    ContractRoute("GET", "/models/{model_id}", ok={404}),
    ContractRoute("POST", "/models/{model_id}/activate", ok={403, 404}),
    ContractRoute("GET", "/stats", ok={200}),
    ContractRoute("GET", "/licenses", ok={200}),
    ContractRoute("POST", "/licenses/resolve", body={"photo_ids": [SAMPLE_UUID]}, ok={200}),
    ContractRoute("GET", "/healthz", ok={200}),
    ContractRoute("GET", "/readyz", ok={200, 503}),
]


class _FakeResult:
    """Resultado mínimo: cualquier agregado devuelve 0."""

    def scalar_one(self) -> int:
        return 0

    def scalar_one_or_none(self) -> None:
        return None

    def one(self) -> tuple[int, int, float]:
        return (0, 0, 0.0)

    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> None:
        return None


class _FakeSession:
    """Sesión que no habla con nada: los agregados salen a cero."""

    async def execute(self, *args: Any, **kwargs: Any) -> _FakeResult:
        return _FakeResult()

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
def test_every_contract_route_is_registered(client: TestClient) -> None:
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
    missing = [(r.method, r.path) for r in CONTRACT_ROUTES if (r.method, r.path) not in registered]
    assert missing == [], f"Rutas del contrato que faltan: {missing}"


@pytest.mark.parametrize("route", CONTRACT_ROUTES, ids=str)
def test_every_contract_route_actually_responds(client: TestClient, route: ContractRoute) -> None:
    """Cada ruta del contrato **responde**, y con el código que le toca.

    Estar registrada no es responder: una tabla de rutas pasaría igual con todos los
    handlers sustituidos por ``raise``. Aquí se invoca cada una de verdad contra una
    base vacía y se exige un código concreto; un 5xx es siempre un fallo.
    """
    response = client.request(route.method, route.url, json=route.body)

    assert response.status_code < 500, (
        f"{route} devolvió {response.status_code}. Ninguna ruta del contrato puede "
        f"reventar con una base vacía: {response.text[:400]}"
    )
    assert response.status_code in route.ok, (
        f"{route} devolvió {response.status_code} y se esperaba "
        f"{sorted(route.ok)}: {response.text[:400]}"
    )


@pytest.mark.parametrize("route", CONTRACT_ROUTES, ids=str)
def test_every_contract_error_is_problem_json(client: TestClient, route: ContractRoute) -> None:
    """Regla dura 4: ningún error sale sin ``application/problem+json``."""
    response = client.request(route.method, route.url, json=route.body)
    if response.status_code < 400:
        return
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE), (
        f"{route} devolvió {response.status_code} con "
        f"content-type {response.headers.get('content-type')!r}"
    )
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail"}


def test_openapi_is_generated_and_is_31(client: TestClient) -> None:
    """El frontend genera sus tipos de aquí (regla dura 7)."""
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.1")
    assert schema["info"]["title"] == "astro-photos API"
    assert len(schema["paths"]) >= 20


def test_stats_returns_zeroes_on_an_empty_repository(client: TestClient) -> None:
    """La portada tiene que poder pintar contadores aunque no haya nada todavía."""
    from app.services.stats import stats_cache

    stats_cache.invalidate()
    response = client.get("/api/v1/stats")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "photo_count",
        "object_count",
        "reconstruction_count",
        "contributor_count",
        "total_exposure_seconds",
    }
    assert body["photo_count"] == 0
    assert "max-age=300" in response.headers["cache-control"]
    stats_cache.invalidate()


def test_me_reports_the_job_limits(client: TestClient) -> None:
    """El cliente debe poder deshabilitar el botón en vez de comerse un 429."""
    body = client.get("/api/v1/me").json()
    quota = body["quota"]
    assert set(quota) >= {
        "quota_bytes",
        "used_bytes",
        "available_bytes",
        "max_queued_jobs",
        "max_jobs_per_day",
        "jobs_queued_now",
        "jobs_today",
    }
    assert quota["max_queued_jobs"] >= 1
    assert quota["jobs_queued_now"] == 0


def test_multipart_completion_rejects_non_consecutive_parts(client: TestClient) -> None:
    response = client.post(
        "/api/v1/photos/22222222-2222-2222-2222-222222222222/uploads/complete-multipart",
        json={
            "upload_id": "u",
            "parts": [{"part_number": 1, "etag": "a"}, {"part_number": 3, "etag": "b"}],
        },
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert "falta" in response.text or "huecos" in response.text


def test_multipart_completion_rejects_an_empty_part_list(client: TestClient) -> None:
    response = client.post(
        "/api/v1/photos/22222222-2222-2222-2222-222222222222/uploads/complete-multipart",
        json={"upload_id": "u", "parts": []},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


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
