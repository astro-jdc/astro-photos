"""Coherencia lectura-tras-escritura: hoy **no** se cumple.

`POST /photos/{id}/complete` responde 200 con la metadata nueva en el cuerpo,
pero la transacción todavía no está confirmada: el único `commit()` de la
petición vive en el teardown de la dependencia `get_session`
(`backend/app/db/session.py`), que corre después de que el handler devuelva.
Un cliente que lea inmediatamente después ve la fila **anterior**.

Impacto directo: el flujo de subida del frontend hace `complete` y navega a la
ficha de la foto. El usuario acaba de poner título y licencia y la ficha le
enseña «Sin título» y la licencia por defecto.

Medido en este entorno: 12 lecturas obsoletas de 15.

Arreglo recomendado: confirmar dentro de la operación de escritura (o en el
handler) antes de devolver la respuesta, en vez de en el teardown. Es un cambio
transversal a todos los endpoints de escritura, así que se deja documentado
aquí en vez de parchearlo a medias.

    backend/.venv/bin/pytest tests/integration/test_read_after_write.py -q -rx
"""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.synthetic import sha256_hex
from tests.invariants.helpers import jpeg_bytes

pytestmark = pytest.mark.integration


def _upload_and_complete(client: httpx.Client, salt: int, title: str, license: str) -> str:
    """Los tres pasos, **sin** esperar a que la escritura se consolide."""
    payload = jpeg_bytes(salt)
    ticket = client.post(
        "/photos/uploads",
        json={
            "filename": f"raw-{salt}.jpg",
            "size_bytes": len(payload),
            "mime_type": "image/jpeg",
            "checksum_sha256": sha256_hex(payload),
        },
    )
    ticket.raise_for_status()
    body = ticket.json()
    post = body["presigned_post"]
    with httpx.Client(timeout=60.0) as raw:
        raw.post(
            post["upload_url"],
            data=post["fields"],
            files={"file": (f"raw-{salt}.jpg", payload, "image/jpeg")},
        )
    done = client.post(
        f"/photos/{body['photo_id']}/complete",
        json={"title": title, "license": license, "location_precision": "hidden"},
    )
    done.raise_for_status()
    # El cuerpo del 200 ya trae la metadata nueva...
    assert done.json()["title"] == title
    assert done.json()["license"]["code"] == license
    return str(body["photo_id"])


@pytest.mark.xfail(
    strict=False,
    reason=(
        "FALLO CONOCIDO: el commit ocurre en el teardown de `get_session`, después "
        "de responder. Una lectura inmediata tras `POST /complete` devuelve la fila "
        "anterior (título nulo y licencia por defecto). Medido: 12/15."
    ),
)
@pytest.mark.parametrize("salt", [9001, 9002, 9003, 9004, 9005])
def test_read_after_write(auth_client: httpx.Client, salt: int) -> None:
    """Lo que el 200 promete tiene que verse en la siguiente lectura."""
    photo_id = _upload_and_complete(auth_client, salt, f"Título {salt}", "CC-BY-4.0")

    detail = auth_client.get(f"/photos/{photo_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()

    assert body["title"] == f"Título {salt}", (
        "La ficha devuelve el título anterior justo después de que `complete` "
        f"respondiera 200 con el nuevo: {body['title']!r}"
    )
    assert body["license"]["code"] == "CC-BY-4.0", (
        f"La ficha devuelve la licencia anterior: {body['license']['code']}"
    )


def test_la_escritura_acaba_siendo_visible(auth_client: httpx.Client) -> None:
    """Control: los datos **sí** llegan; el problema es solo cuándo.

    Sin este test, el anterior podría estar señalando una pérdida de datos, que
    sería mucho más grave. Esto acota el fallo a un problema de visibilidad.
    """
    import time

    photo_id = _upload_and_complete(auth_client, 9100, "Consolidado", "CC-BY-SA-4.0")
    for _ in range(40):
        body = auth_client.get(f"/photos/{photo_id}").json()
        if body["title"] == "Consolidado":
            assert body["license"]["code"] == "CC-BY-SA-4.0"
            return
        time.sleep(0.05)
    pytest.fail("La escritura nunca llegó a ser visible: esto ya no es una carrera.")
