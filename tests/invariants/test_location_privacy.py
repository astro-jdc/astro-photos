"""Una foto `hidden` no filtra coordenadas por **ningún** camino.

El test obvio (¿sale `location: null` en `GET /photos/{id}`?) lo escribe
cualquiera. Los que importan son los otros: el EXIF del fichero que se sirve, el
mapa de cobertura, la búsqueda geoespacial, la procedencia de una reconstrucción.
Basta con que uno solo de esos caminos publique la posición para que la promesa
de privacidad sea falsa, y el autor no tiene forma de enterarse.

Se prueba con un punto reconocible (el Observatorio del Teide) para que un fallo
se vea leyendo la salida, no interpretando un delta numérico.

    backend/.venv/bin/pytest tests/invariants/test_location_privacy.py -q
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.helpers.synthetic import read_gps
from tests.integration.s3 import get_object
from tests.invariants.helpers import (
    TEIDE_LAT,
    TEIDE_LON,
    create_ready_photo,
    ensure_sky_object,
    mark_ready,
)

pytestmark = pytest.mark.invariant

#: Fragmentos de las coordenadas del Teide que no pueden aparecer en ninguna
#: respuesta sobre una foto oculta. Se comprueban como texto sobre el JSON
#: entero: así el test no depende de por qué campo se filtraría.
LEAK_MARKERS = ("28.300", "-16.512", "28.3002", "16.5123")


def assert_no_leak(payload: Any, *, where: str) -> None:
    """Ni las coordenadas ni nada que se les parezca, en ninguna parte del JSON."""
    blob = json.dumps(payload, ensure_ascii=False)
    for marker in LEAK_MARKERS:
        assert marker not in blob, (
            f"FUGA DE UBICACIÓN en {where}: aparece {marker!r} para una foto con "
            f"location_precision='hidden'.\n{blob[:1500]}"
        )


@pytest.fixture
def hidden_photo(auth_client: httpx.Client) -> str:
    """Foto en el Teide, con la ubicación marcada como oculta, y ya `ready`."""
    photo_id = create_ready_photo(
        auth_client,
        license="CC-BY-4.0",
        location_precision="hidden",
        lat=TEIDE_LAT,
        lon=TEIDE_LON,
        title="Oculta",
    )
    mark_ready(photo_id)
    return photo_id


@pytest.fixture
def exact_photo(auth_client: httpx.Client) -> str:
    """Control: la misma posición pero publicada. Si este no ve nada, el test miente."""
    photo_id = create_ready_photo(
        auth_client,
        license="CC-BY-4.0",
        location_precision="exact",
        lat=TEIDE_LAT,
        lon=TEIDE_LON,
        title="Publica",
    )
    mark_ready(photo_id)
    return photo_id


# --------------------------------------------------------------------------- #
# Control negativo: sin esto, todos los demás podrían estar pasando por vacíos.
# --------------------------------------------------------------------------- #
def test_el_control_publica_la_posicion(client: httpx.Client, exact_photo: str) -> None:
    """Con `exact` las coordenadas **sí** salen. Prueba de que el test detecta fugas."""
    body = client.get(f"/photos/{exact_photo}").json()
    blob = json.dumps(body)
    assert any(m in blob for m in LEAK_MARKERS), (
        "Ni con location_precision='exact' salen las coordenadas: el test de "
        "privacidad no estaría comprobando nada."
    )


# --------------------------------------------------------------------------- #
# Camino 1: la respuesta de la API, anónima y autenticada.
# --------------------------------------------------------------------------- #
def test_el_detalle_publico_no_filtra(client: httpx.Client, hidden_photo: str) -> None:
    body = client.get(f"/photos/{hidden_photo}").json()
    assert body["location"] is None, f"location debería ser null, es {body['location']}"
    assert_no_leak(body, where="GET /photos/{id} anónimo")


def test_el_detalle_para_un_tercero_autenticado_no_filtra(
    api_base: str, other_user, hidden_photo: str
) -> None:
    """Estar logueado no da acceso: solo el dueño ve su propia posición."""
    with httpx.Client(base_url=api_base, headers=other_user.headers, timeout=30.0) as intruder:
        body = intruder.get(f"/photos/{hidden_photo}").json()
    assert body["location"] is None
    assert_no_leak(body, where="GET /photos/{id} como tercero autenticado")


def test_la_busqueda_no_filtra(client: httpx.Client, user, hidden_photo: str) -> None:
    """La foto oculta sigue siendo buscable; lo que no sale es su posición.

    Se filtra por `owner` para acotar la página a las fotos de este test: sin
    eso, el resultado depende de cuántas fotos haya acumulado la base y la
    aserción se vuelve inestable según el orden de ejecución.
    """
    body = client.get("/photos", params={"owner": user.id, "limit": 200}).json()
    hit = next((p for p in body["items"] if p["id"] == hidden_photo), None)
    assert hit is not None, (
        "la foto oculta debe seguir apareciendo en la búsqueda de su propietario"
    )
    assert hit["location"] is None
    assert_no_leak(hit, where="GET /photos (búsqueda)")


def test_la_busqueda_geoespacial_no_confirma_la_posicion(
    client: httpx.Client, user, hidden_photo: str
) -> None:
    """`?near=` sigue usando la posición real para filtrar (así está diseñado),
    pero la respuesta no puede devolverla."""
    body = client.get(
        "/photos",
        params={
            "near": f"{TEIDE_LAT},{TEIDE_LON}",
            "km": 5,
            "owner": user.id,
            "limit": 200,
        },
    ).json()
    # Se comprueba **solo** la entrada de la foto oculta: otras fotos del mismo
    # sitio con precisión `exact` sí publican su posición, y eso es correcto.
    hit = next((p for p in body["items"] if p["id"] == hidden_photo), None)
    if hit is not None:
        assert hit["location"] is None
        assert_no_leak(hit, where="GET /photos?near= (entrada de la foto oculta)")


def test_los_similares_no_filtran(client: httpx.Client, hidden_photo: str) -> None:
    resp = client.get(f"/photos/similar/{hidden_photo}")
    if resp.status_code == 200:
        assert_no_leak(resp.json(), where="GET /photos/similar/{id}")


# --------------------------------------------------------------------------- #
# Camino 2: el EXIF del fichero servido.
# --------------------------------------------------------------------------- #
def test_el_fichero_original_conserva_el_gps_solo_en_el_bucket_privado(
    auth_client: httpx.Client, hidden_photo: str
) -> None:
    """El original es inmutable y **no se sirve directo** (`docs/data-model.md`).

    Guardar el EXIF completo ahí es correcto: el pipeline necesita el GPS real
    para airmass y rotación de campo. Lo que no puede es salir por la API.
    """
    detail = auth_client.get(f"/photos/{hidden_photo}").json()
    assert detail["preview_url"] is None or "s3_key_original" not in json.dumps(detail)


def test_la_descarga_de_una_foto_oculta_no_publica_gps_en_cabeceras(
    auth_client: httpx.Client, hidden_photo: str
) -> None:
    """Ni la cabecera de atribución ni ninguna otra pueden llevar coordenadas."""
    resp = auth_client.get(f"/photos/{hidden_photo}/download", follow_redirects=False)
    assert resp.status_code in (302, 200), resp.text
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert_no_leak(headers, where="cabeceras de GET /photos/{id}/download")


@pytest.mark.xfail(
    strict=False,
    reason=(
        "No hay worker de ingesta corriendo en el entorno de test, así que no se "
        "generan preview ni thumb y no se puede comprobar que su EXIF va limpio. "
        "El test queda escrito para cuando el worker forme parte del stack local."
    ),
)
def test_la_preview_servida_no_lleva_gps(
    auth_client: httpx.Client, hidden_photo: str
) -> None:
    """La preview que sí se sirve tiene que ir sin EXIF de GPS."""
    detail = auth_client.get(f"/photos/{hidden_photo}").json()
    preview_url = detail["preview_url"]
    assert preview_url, "sin preview generada no se puede comprobar su EXIF"
    with httpx.Client(timeout=60.0, follow_redirects=True) as raw:
        payload = raw.get(preview_url).content
    assert read_gps(payload) == {}, (
        "La preview servida conserva el bloque GPS del EXIF original."
    )


def test_el_binario_subido_si_llevaba_gps(auth_client: httpx.Client, hidden_photo: str) -> None:
    """Prueba de que el fichero de partida **sí** tenía GPS.

    Sin esto, un `read_gps(...) == {}` en los tests de arriba no probaría nada:
    podría ser que la imagen nunca hubiera tenido coordenadas.
    """
    from tests.invariants.helpers import jpeg_bytes

    assert read_gps(jpeg_bytes(0)), "el JPEG de prueba tiene que llevar GPS de verdad"


# --------------------------------------------------------------------------- #
# Camino 3: el mapa de cobertura (`cells[]` y `sites[]`).
# --------------------------------------------------------------------------- #
def test_el_mapa_de_cobertura_no_publica_la_banda_de_latitud(
    client: httpx.Client, auth_client: httpx.Client
) -> None:
    """`docs/api.md`: las ocultas van a `lat_bin = -999`, no a su banda real.

    Una banda de 15° sigue siendo una posición (~1700 km) y su autor no
    autorizó publicarla ni redondeada.
    """
    object_id = ensure_sky_object()

    photo_id = create_ready_photo(
        auth_client, location_precision="hidden", lat=TEIDE_LAT, lon=TEIDE_LON
    )
    mark_ready(photo_id)
    auth_client.patch(f"/photos/{photo_id}", json={"object_id": object_id})

    coverage = client.get(f"/objects/{object_id}/coverage").json()
    assert_no_leak(coverage, where="GET /objects/{id}/coverage")

    teide_band = int(TEIDE_LAT // 15) * 15
    for cell in coverage.get("cells") or []:
        assert cell["lat_bin"] != teide_band or cell["count"] == 0, (
            "Una foto oculta está aportando su banda de latitud real a `cells[]`; "
            f"debería ir a la banda desconocida (-999). cells={coverage['cells']}"
        )


def test_los_sites_del_mapa_no_llevan_posiciones_ocultas(
    client: httpx.Client, auth_client: httpx.Client
) -> None:
    """`sites[]` se agrupa por precisión **antes** de agregar (`docs/api.md`).

    El objeto es exclusivo de este test y **solo** tiene fotos ocultas, así que
    cualquier punto que aparezca en `sites[]` es necesariamente una fuga: no
    hay ninguna otra foto de la que pudiera venir.
    """
    object_id = ensure_sky_object()
    hidden = [
        create_ready_photo(
            auth_client, location_precision="hidden", lat=TEIDE_LAT, lon=TEIDE_LON
        )
        for _ in range(2)
    ]
    mark_ready(*hidden)
    for pid in hidden:
        auth_client.patch(f"/photos/{pid}", json={"object_id": object_id})

    coverage = client.get(f"/objects/{object_id}/coverage").json()
    sites = coverage.get("sites") or []
    assert sites == [], (
        "Este objeto solo tiene fotos con location_precision='hidden' y aun así "
        f"`sites[]` publica puntos: {sites}"
    )
    assert_no_leak(coverage, where="GET /objects/{id}/coverage (solo fotos ocultas)")


def test_una_foto_oculta_no_contamina_el_punto_de_una_publica(
    client: httpx.Client, auth_client: httpx.Client
) -> None:
    """Agrupar por precisión antes de agregar, no después.

    Si se agregase primero y se ofuscase el centroide después, el punto de una
    foto `city` se desplazaría hacia la posición exacta de la oculta con la que
    se promedió: la posición protegida se filtraría por la puerta de atrás.
    """
    object_id = ensure_sky_object()
    hidden = create_ready_photo(
        auth_client, location_precision="hidden", lat=TEIDE_LAT, lon=TEIDE_LON
    )
    city = create_ready_photo(
        auth_client, location_precision="city", lat=TEIDE_LAT + 4.0, lon=TEIDE_LON + 4.0
    )
    mark_ready(hidden, city)
    for pid in (hidden, city):
        auth_client.patch(f"/photos/{pid}", json={"object_id": object_id})

    coverage = client.get(f"/objects/{object_id}/coverage").json()
    for site in coverage.get("sites") or []:
        assert site["precision"] != "hidden", f"`sites[]` publica un punto oculto: {site}"
    assert_no_leak(coverage.get("sites") or [], where="coverage.sites[] con mezcla")


# --------------------------------------------------------------------------- #
# Camino 4: procedencia y estadísticas.
# --------------------------------------------------------------------------- #
def test_la_procedencia_de_una_reconstruccion_no_filtra(
    client: httpx.Client, auth_client: httpx.Client, hidden_photo: str
) -> None:
    """`GET /reconstructions/{id}/inputs` publica pesos y licencias, no posiciones."""
    listing = client.get("/reconstructions", params={"limit": 10}).json()
    for job in listing["items"]:
        inputs = client.get(f"/reconstructions/{job['id']}/inputs")
        if inputs.status_code == 200:
            assert_no_leak(inputs.json(), where="GET /reconstructions/{id}/inputs")


def test_las_estadisticas_no_filtran(client: httpx.Client, hidden_photo: str) -> None:
    assert_no_leak(client.get("/stats").json(), where="GET /stats")


def test_el_perfil_publico_del_autor_no_filtra(
    client: httpx.Client, user, hidden_photo: str
) -> None:
    resp = client.get(f"/users/{user.id}")
    if resp.status_code == 200:
        assert_no_leak(resp.json(), where="GET /users/{id}")


# --------------------------------------------------------------------------- #
# La escalera completa, no solo `hidden`.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("precision", "expect_exact"),
    [("exact", True), ("city", False), ("country", False), ("hidden", False)],
)
def test_solo_exact_publica_la_posicion_real(
    client: httpx.Client, auth_client: httpx.Client, precision: str, expect_exact: bool
) -> None:
    """`city` redondea a 0.1° y `country` da un centroide: ninguna es la real."""
    photo_id = create_ready_photo(
        auth_client, location_precision=precision, lat=TEIDE_LAT, lon=TEIDE_LON
    )
    mark_ready(photo_id)
    body = client.get(f"/photos/{photo_id}").json()
    blob = json.dumps(body)
    leaked = any(m in blob for m in LEAK_MARKERS)
    assert leaked is expect_exact, (
        f"Con location_precision={precision!r} la posición exacta "
        f"{'debería' if expect_exact else 'NO debería'} publicarse. location="
        f"{body['location']}"
    )
