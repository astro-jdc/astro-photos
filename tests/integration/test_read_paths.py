"""Regresión: los caminos de lectura que devolvían 500 contra la base real.

Los tres fallos que fija este fichero pasaron los 681 tests del backend porque
esas pruebas usan dobles en memoria: nunca ejercitan la semántica real del
mapa de identidad de SQLAlchemy ni la serialización de cabeceras HTTP. Solo
aparecen contra Postgres de verdad, que es lo que hace este fichero.

1. `GET /photos/{id}` — 500 en **toda** foto visible. Un `UPDATE` masivo para
   el contador de visitas expiraba el objeto `Photo` recién cargado y la
   serialización disparaba IO perezosa fuera del greenlet (`MissingGreenlet`).
2. `GET /photos/{id}/download` — el mismo problema, y además la licencia nunca
   se congelaba porque la escritura de `license_locked_at` caía sobre el objeto
   expirado.
3. `GET /photos/{id}/download` — la cabecera `X-Attribution` lleva una raya
   (U+2014) fija en la plantilla, que no es codificable en latin-1: toda
   descarga reventaba al serializar la cabecera.

    backend/.venv/bin/pytest tests/integration/test_read_paths.py -q
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.invariants.helpers import create_ready_photo, mark_ready

pytestmark = pytest.mark.integration


def test_el_detalle_de_una_foto_no_revienta(auth_client: httpx.Client) -> None:
    """El endpoint más visitado del producto. Devolvía 500 siempre."""
    photo_id = create_ready_photo(auth_client, license="CC-BY-4.0")
    resp = auth_client.get(f"/photos/{photo_id}")
    assert resp.status_code == 200, (
        f"GET /photos/{{id}} devolvió {resp.status_code}: {resp.text[:400]}"
    )
    assert resp.json()["id"] == photo_id


def test_el_contador_de_visitas_sube(auth_client: httpx.Client) -> None:
    """Y el contador sigue funcionando: el arreglo no lo desactivó.

    Sin esto, `synchronize_session=False` podría haberse sustituido por "no
    contar nada" y el test anterior seguiría en verde.
    """
    photo_id = create_ready_photo(auth_client, license="CC-BY-4.0")
    auth_client.get(f"/photos/{photo_id}")
    auth_client.get(f"/photos/{photo_id}")
    time.sleep(0.3)
    assert auth_client.get(f"/photos/{photo_id}").json()["view_count"] >= 2


def test_la_descarga_no_revienta(auth_client: httpx.Client) -> None:
    """302 a la URL firmada, con las cabeceras de crédito."""
    photo_id = create_ready_photo(auth_client, license="CC-BY-4.0")
    resp = auth_client.get(f"/photos/{photo_id}/download", follow_redirects=False)
    assert resp.status_code == 302, (
        f"GET /photos/{{id}}/download devolvió {resp.status_code}: {resp.text[:400]}"
    )
    assert resp.headers["X-License"] == "CC-BY-4.0"
    assert resp.headers["X-Attribution"]


def test_la_descarga_aguanta_un_titulo_con_acentos(auth_client: httpx.Client) -> None:
    """La cabecera de atribución no puede depender de que el texto sea ASCII.

    Este producto es de astrofotógrafos de todo el mundo: los nombres y los
    títulos llevan acentos, y la línea de crédito lleva una raya U+2014 fija.
    """
    photo_id = create_ready_photo(
        auth_client, license="CC-BY-4.0", title="M31 — la galaxía de Andrómeda"
    )
    resp = auth_client.get(f"/photos/{photo_id}/download", follow_redirects=False)
    assert resp.status_code == 302, (
        f"Un título con acentos rompe la descarga: {resp.status_code} {resp.text[:300]}"
    )
    # El valor sigue siendo recuperable, aunque vaya percent-codificado.
    from urllib.parse import unquote

    assert "Andrómeda" in unquote(resp.headers["X-Attribution"])


def test_la_descarga_por_json_devuelve_el_texto_legible(auth_client: httpx.Client) -> None:
    """`?redirect=false` no tiene la limitación de las cabeceras: texto tal cual."""
    photo_id = create_ready_photo(
        auth_client, license="CC-BY-4.0", title="M31 — la galaxía de Andrómeda"
    )
    resp = auth_client.get(f"/photos/{photo_id}/download", params={"redirect": "false"})
    assert resp.status_code == 200, resp.text
    assert "M31 — la galaxía de Andrómeda" in resp.json()["attribution"]


def test_una_descarga_de_un_tercero_congela_la_licencia(
    auth_client: httpx.Client, api_base: str, other_user
) -> None:
    """`docs/licensing.md`: a partir de la primera descarga ajena, solo se relaja.

    Esta escritura caía sobre el objeto expirado, así que la regla de congelado
    no se aplicaba nunca. Se comprueba el efecto, no la implementación.
    """
    photo_id = create_ready_photo(auth_client, license="CC-BY-4.0")
    mark_ready(photo_id)
    assert auth_client.get(f"/photos/{photo_id}").json()["license"]["locked_at"] is None

    with httpx.Client(base_url=api_base, headers=other_user.headers, timeout=30.0) as third:
        assert third.get(f"/photos/{photo_id}/download", follow_redirects=False).status_code == 302

    time.sleep(0.5)
    assert auth_client.get(f"/photos/{photo_id}").json()["license"]["locked_at"] is not None, (
        "Una descarga por un tercero no congeló la licencia."
    )

    # Y a partir de ahí endurecerla se rechaza, pero relajarla se acepta.
    assert auth_client.patch(f"/photos/{photo_id}", json={"license": "ARR"}).status_code == 422
    assert auth_client.patch(f"/photos/{photo_id}", json={"license": "CC0-1.0"}).status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "dec=0",
        "ra=10",
        "radius=2",
        "min_focal=100&max_focal=50",
        "from=2027-01-01&to=2020-01-01",
        "sort=nearest",
    ],
)
def test_una_busqueda_incoherente_es_422_y_no_500(client: httpx.Client, query: str) -> None:
    """Un filtro mal combinado es culpa del usuario, no del servidor.

    `PhotoSearchQuery` se valida a mano dentro del handler, así que la
    `ValidationError` de pydantic no pasaba por el manejador de FastAPI y salía
    como 500 — ensuciando además las alarmas de error interno.
    """
    resp = client.get(f"/photos?{query}")
    assert resp.status_code == 422, (
        f"GET /photos?{query} devolvió {resp.status_code}, se esperaba 422: "
        f"{resp.text[:300]}"
    )
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_una_busqueda_coherente_sigue_funcionando(client: httpx.Client) -> None:
    """Control: el cono completo no se rechaza."""
    assert client.get("/photos?ra=10&dec=41&radius=2").status_code == 200
