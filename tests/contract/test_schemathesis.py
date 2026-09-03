"""Schemathesis contra las rutas públicas del OpenAPI.

Genera entradas a partir del propio schema y comprueba que el servidor no se
rompe y que responde lo que dice su contrato. Solo rutas 🔓: las autenticadas
darían 401 y el fuzzing no aportaría nada.

Lo que se busca aquí no son 4xx —son legítimos— sino:
  * 500 con cualquier entrada que el schema declara válida,
  * respuestas que no encajan con el schema que el propio OpenAPI promete,
  * errores que no salen como `application/problem+json`.

    backend/.venv/bin/pytest tests/contract/test_schemathesis.py -q
"""

from __future__ import annotations

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi import checks as openapi_checks

from tests.conftest import API_V1

pytestmark = pytest.mark.contract

#: Rutas públicas de `docs/api.md` (las marcadas 🔓) que aceptan fuzzing barato.
PUBLIC_PATHS = (
    "/api/v1/healthz",
    "/api/v1/readyz",
    "/api/v1/stats",
    "/api/v1/licenses",
    "/api/v1/photos",
    "/api/v1/objects",
    "/api/v1/reconstructions",
    "/api/v1/models",
)


def _content_type(response: object) -> str:
    """`content-type` como cadena.

    Schemathesis normaliza las cabeceras a listas de valores; httpx las da como
    cadena. Se admiten las dos para que el test no dependa del transporte.
    """
    raw = getattr(response, "headers", {}).get("content-type", "")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    return str(raw)


def _load_schema() -> schemathesis.BaseSchema:
    return schemathesis.openapi.from_url(f"{API_V1}/openapi.json")


try:
    schema = _load_schema()
except Exception as exc:  # pragma: no cover - el backend no está levantado
    schema = None
    _LOAD_ERROR: Exception | None = exc
else:
    _LOAD_ERROR = None


pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        schema is None,
        reason=f"No se pudo cargar el OpenAPI ({_LOAD_ERROR}). Levanta el backend.",
    ),
]


if schema is not None:

    @schema.include(path_regex=r"^/api/v1/(healthz|readyz|stats|licenses|photos|objects|reconstructions|models)$").parametrize()
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    def test_rutas_publicas(case: schemathesis.Case) -> None:
        """Ninguna entrada válida según el schema debe producir un 500.

        Los checks de autenticación de schemathesis van desactivados a
        propósito: el OpenAPI marca varias rutas 🔓 como si exigieran `Bearer`
        (ver :func:`test_las_rutas_publicas_no_exigen_autenticacion` y
        :func:`test_el_openapi_no_deberia_marcar_como_privadas_las_rutas_publicas`),
        de modo que esos checks fallarían por el defecto de anotación, no por
        un fallo de comportamiento — y taparían los 500 que sí buscamos.
        """
        response = case.call()

        assert response.status_code < 500, (
            f"{case.method} {case.path} devolvió {response.status_code} con "
            f"{case.query or case.body!r}:\n{response.text[:800]}"
        )

        # El contrato dice RFC 9457 para *todos* los errores.
        if response.status_code >= 400:
            ctype = _content_type(response)
            assert ctype.startswith("application/problem+json"), (
                f"{case.method} {case.path} devolvió {response.status_code} con "
                f"content-type {ctype!r}; `docs/api.md` exige application/problem+json."
            )

        case.validate_response(
            response,
            excluded_checks=[
                # Ver el defecto de anotación de seguridad, más abajo.
                openapi_checks.ignored_auth,
                openapi_checks.missing_required_header,
                # `positive/negative_data_rejection`: el schema declara varios
                # parámetros como `string` a secas (`cursor`, `near`, `object`)
                # mientras el servidor exige un formato concreto y contesta 400
                # en problem+json. Comprobado a mano: el comportamiento es
                # correcto; lo que está flojo es el tipo del OpenAPI, no el
                # servidor. Se anota como nit en el informe, no como fallo.
                openapi_checks.negative_data_rejection,
                openapi_checks.positive_data_acceptance,
                # OPTIONS lo contesta el middleware de CORS, que no anuncia los
                # métodos documentados en su `Allow`. No afecta a ningún cliente.
                openapi_checks.allow_header_conformance,
            ],
        )


# --------------------------------------------------------------------------- #
# El defecto que destapó el fuzzing, fijado con dos tests que no se pueden
# borrar sin darse cuenta.
# --------------------------------------------------------------------------- #
PUBLIC_IN_DOCS = ("/licenses", "/stats", "/photos", "/objects", "/reconstructions", "/models")


@pytest.mark.parametrize("route", PUBLIC_IN_DOCS)
def test_las_rutas_publicas_no_exigen_autenticacion(client, route: str) -> None:
    """Comportamiento real: las rutas 🔓 responden sin token. Esto sí pasa."""
    resp = client.get(route)
    assert resp.status_code == 200, (
        f"GET {route} está marcada 🔓 en `docs/api.md` pero devolvió "
        f"{resp.status_code} sin token: {resp.text[:300]}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECTO CONOCIDO: `optional_user` comparte el `bearer_scheme` con "
        "`current_user`, así que FastAPI anota las rutas 🔓 con "
        "`security: [{Bearer: []}]`. Funcionan sin token, pero el OpenAPI dice "
        "lo contrario y cualquier cliente generado se lo cree. Arreglarlo pide "
        "un esquema de seguridad aparte para el usuario opcional."
    ),
)
@pytest.mark.parametrize("route", ["/licenses", "/photos", "/reconstructions"])
def test_el_openapi_no_deberia_marcar_como_privadas_las_rutas_publicas(
    openapi: dict, route: str
) -> None:
    """El OpenAPI debe declarar públicas las rutas que `docs/api.md` marca 🔓."""
    operation = openapi["paths"][f"/api/v1{route}"]["get"]
    assert not operation.get("security"), (
        f"GET {route} es 🔓 en `docs/api.md` pero el OpenAPI declara "
        f"security={operation.get('security')}."
    )
