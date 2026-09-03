"""Crear fotos reales a través de la API, para los tests de invariantes.

Van por el flujo de 3 pasos de verdad (ticket -> MinIO -> complete), no por
INSERT directo: así lo que se prueba después es lo que un usuario obtendría.
"""

from __future__ import annotations

import functools
from typing import Any

import httpx

from tests.helpers.synthetic import (
    TEIDE_LAT,
    TEIDE_LON,
    encode_jpeg_with_gps,
    make_star_field,
    sha256_hex,
)

__all__ = [
    "TEIDE_LAT",
    "TEIDE_LON",
    "create_ready_photo",
    "ensure_sky_object",
    "jpeg_bytes",
    "mark_ready",
]

_ENSURE_OBJECT = """
import asyncio, sys
from sqlalchemy import text
from app.db.session import get_engine

number = sys.argv[1]

async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        found = (await conn.execute(
            text("SELECT id FROM sky_objects WHERE catalog = 'NGC' AND catalog_number = :n"),
            {"n": number},
        )).scalar()
        if found is None:
            found = (await conn.execute(
                text(
                    "INSERT INTO sky_objects "
                    "(catalog, catalog_number, common_name, object_type, ra_deg, dec_deg, "
                    " aliases) VALUES ('NGC', :n, :name, 'galaxy', 10.6847, 41.269, "
                    " ARRAY[]::text[]) RETURNING id"
                ),
                {"n": number, "name": f"QA {number}"},
            )).scalar_one()
    await engine.dispose()
    print(str(found))

asyncio.run(main())
"""


def ensure_sky_object(catalog_number: str | None = None) -> str:
    """Devuelve el id de un objeto del catálogo, sembrándolo si hace falta.

    Cada test que mira el mapa de cobertura pide **su propio** objeto: así las
    fotos de un test no contaminan las cuentas de otro y una fuga se puede
    afirmar sin ambigüedad ("este objeto solo tiene fotos ocultas, luego
    cualquier punto en `sites[]` es una fuga").

    `make seed` apunta a `scripts/seed_dev.py`, que no existe en el repo, así
    que sembrar aquí evita que estos tests se salten en silencio — y un test de
    privacidad que se salta en silencio es peor que ninguno.
    """
    import subprocess
    import uuid

    import pytest

    from tests.conftest import BACKEND_PY, REPO_ROOT

    number = catalog_number or uuid.uuid4().hex[:10]
    proc = subprocess.run(
        [str(BACKEND_PY), "-c", _ENSURE_OBJECT, number],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "backend"),
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"No se pudo sembrar el objeto de prueba:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1]

_MARK_READY = """
import asyncio, sys
from sqlalchemy import text
from app.db.session import get_engine

async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE photos SET status = 'ready' WHERE id = ANY(:ids)"),
            {"ids": [x for x in sys.argv[1:]]},
        )
    await engine.dispose()

asyncio.run(main())
"""


def mark_ready(*photo_ids: str) -> None:
    """Pasa las fotos a `status='ready'`.

    En producción lo hace el worker de ingesta al terminar; aquí no hay worker,
    y sin `ready` la foto no es visible para nadie más que su dueño — que es lo
    que hay que poder probar en los tests de privacidad.
    """
    import subprocess

    import pytest

    from tests.conftest import BACKEND_PY, REPO_ROOT

    proc = subprocess.run(
        [str(BACKEND_PY), "-c", _MARK_READY, *photo_ids],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "backend"),
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"No se pudo marcar ready:\n{proc.stdout}\n{proc.stderr}")


@functools.lru_cache(maxsize=8)
def jpeg_bytes(salt: int = 0) -> bytes:
    """JPEG sintético con GPS del Teide.

    `salt` cambia la semilla del ruido para que el checksum sea distinto: el
    backend deduplica por `checksum_sha256` dentro del mismo propietario, así
    que dos fotos idénticas no se pueden subir dos veces.
    """
    return encode_jpeg_with_gps(make_star_field(seed=20260903 + salt))


_counter = 0


def create_ready_photo(
    client: httpx.Client,
    *,
    license: str = "CC-BY-4.0",
    location_precision: str = "exact",
    lat: float = TEIDE_LAT,
    lon: float = TEIDE_LON,
    elevation_m: float | None = 2390.0,
    allow_derivatives_in_stacks: bool = True,
    allow_ai_training: bool = True,
    title: str = "Foto de prueba",
    with_location: bool = True,
) -> str:
    """Sube una foto entera y devuelve su `photo_id`.

    Recorre los tres pasos del contrato, incluido el POST real a MinIO.
    """
    global _counter
    _counter += 1
    payload = jpeg_bytes(_counter)

    ticket = client.post(
        "/photos/uploads",
        json={
            "filename": f"qa-{_counter}.jpg",
            "size_bytes": len(payload),
            "mime_type": "image/jpeg",
            "checksum_sha256": sha256_hex(payload),
        },
    )
    ticket.raise_for_status()
    body = ticket.json()
    post = body["presigned_post"]

    with httpx.Client(timeout=60.0) as raw:
        upload = raw.post(
            post["upload_url"],
            data=post["fields"],
            files={"file": (f"qa-{_counter}.jpg", payload, "image/jpeg")},
        )
    assert upload.status_code in (200, 204), f"MinIO rechazó la subida: {upload.text}"

    complete: dict[str, Any] = {
        "title": title,
        "license": license,
        "captured_at_local": "2026-02-14T23:41:07",
        "utc_offset_minutes": 0,
        "location_precision": location_precision,
        "allow_ai_training": allow_ai_training,
        "allow_derivatives_in_stacks": allow_derivatives_in_stacks,
        "equipment": {"focal_length_mm": 600.0, "focal_ratio": 5.6, "exposure_seconds": 120.0},
    }
    if with_location:
        complete["location"] = {
            "lat": lat,
            "lon": lon,
            "accuracy_m": 5.0,
            "elevation_m": elevation_m,
        }

    done = client.post(f"/photos/{body['photo_id']}/complete", json=complete)
    done.raise_for_status()
    photo_id = str(body["photo_id"])
    _wait_until_visible(client, photo_id, license)
    return photo_id


def _wait_until_visible(client: httpx.Client, photo_id: str, license: str) -> None:
    """Espera a que `POST /complete` sea visible para una lectura posterior.

    **No es paranoia de test: tapa un fallo real del backend.** El único commit
    de la petición vive en el teardown de la dependencia `get_session`, que
    corre *después* de que el handler devuelva, así que `POST /complete`
    responde 200 con la metadata nueva mientras la transacción sigue sin
    confirmar. Una lectura inmediata ve la fila anterior (medido: 12 de 15
    veces). Ver `test_read_after_write` en
    `tests/integration/test_read_after_write.py`, que documenta el fallo.

    Aquí se espera para que los tests que solo *usan* una foto no hereden esa
    inestabilidad; el fallo se afirma en su propio test, no se esconde.
    """
    import time

    for _ in range(40):
        resp = client.get(f"/photos/{photo_id}")
        if resp.status_code == 200 and resp.json()["license"]["code"] == license:
            return
        time.sleep(0.05)
