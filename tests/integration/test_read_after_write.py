"""Coherencia lectura-tras-escritura.

Lo que un 200 promete tiene que ser visible en la lectura siguiente, desde
cualquier conexión. Antes no lo era: el único `commit()` de la petición vivía en
el teardown de `get_session`, que corre **después** de emitir la respuesta, así
que el frontend hacía `complete`, navegaba a la ficha y le enseñaba al usuario
«Sin título» con la licencia por defecto — justo la que acababa de cambiar.
Medido entonces: 12 lecturas obsoletas de 15.

Ahora la unidad de trabajo es la petición y se confirma antes de emitir la
respuesta (`backend/app/core/uow.py`). Medido después: 0 de 120.

    backend/.venv/bin/pytest tests/integration/test_read_after_write.py -q
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
