"""El flujo de subida completo contra MinIO real.

Es la funcionalidad central del producto: si esto no funciona, no hay producto.
Por eso el test sube el binario **de verdad** a la URL presignada en vez de
simular boto3, y luego comprueba en S3 que el objeto está y que sus bytes son
exactamente los que se mandaron.

    backend/.venv/bin/pytest tests/integration/test_upload_flow.py -q
"""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.synthetic import (
    TEIDE_LAT,
    TEIDE_LON,
    encode_jpeg_with_gps,
    make_star_field,
    read_gps,
    sha256_hex,
)
from tests.integration.conftest import ApiUser

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def jpeg() -> bytes:
    """Un JPEG sintético con estrellas gaussianas y GPS del Teide en el EXIF."""
    payload = encode_jpeg_with_gps(make_star_field())
    assert read_gps(payload), "el fichero de prueba tiene que llevar GPS de verdad"
    return payload


def request_ticket(client: httpx.Client, payload: bytes, filename: str = "m31.jpg") -> dict:
    """Paso 1: `POST /photos/uploads`."""
    r = client.post(
        "/photos/uploads",
        json={
            "filename": filename,
            "size_bytes": len(payload),
            "mime_type": "image/jpeg",
            "checksum_sha256": sha256_hex(payload),
        },
    )
    assert r.status_code == 201, f"paso 1 falló: {r.status_code} {r.text}"
    return dict(r.json())


def put_binary(ticket: dict, payload: bytes, filename: str = "m31.jpg") -> httpx.Response:
    """Paso 2: subida real del binario al POST presignado. Sin backend de por medio."""
    post = ticket["presigned_post"]
    assert post is not None, f"se esperaba un presignado simple, llegó {ticket}"
    files = {"file": (filename, payload, "image/jpeg")}
    with httpx.Client(timeout=60.0) as raw:
        return raw.post(post["upload_url"], data=post["fields"], files=files)


def complete(client: httpx.Client, photo_id: str, **overrides) -> httpx.Response:
    """Paso 3: `POST /photos/{id}/complete`."""
    body = {
        "title": "M31 sintética",
        "license": "CC-BY-4.0",
        "captured_at_local": "2026-02-14T23:41:07",
        "utc_offset_minutes": 0,
        "location": {"lat": TEIDE_LAT, "lon": TEIDE_LON, "accuracy_m": 5.0, "elevation_m": 2390.0},
        "location_precision": "exact",
        "equipment": {"focal_length_mm": 600.0, "focal_ratio": 5.6, "exposure_seconds": 120.0},
    }
    body.update(overrides)
    return client.post(f"/photos/{photo_id}/complete", json=body)


# --------------------------------------------------------------------------- #
def test_flujo_de_subida_de_punta_a_punta(auth_client: httpx.Client, jpeg: bytes) -> None:
    """1) ticket -> 2) binario a MinIO -> 3) complete. Los tres pasos, de verdad."""
    ticket = request_ticket(auth_client, jpeg)
    photo_id = ticket["photo_id"]

    # El contrato dice que el binario no pasa por el backend: el presignado
    # tiene que apuntar a S3/MinIO, no a nosotros.
    upload_url = ticket["presigned_post"]["upload_url"]
    assert "8000" not in upload_url, f"el presignado apunta al backend: {upload_url}"

    upload = put_binary(ticket, jpeg)
    assert upload.status_code in (200, 204), (
        f"MinIO rechazó la subida: {upload.status_code}\n{upload.text}"
    )

    done = complete(auth_client, photo_id)
    assert done.status_code == 200, f"paso 3 falló: {done.status_code} {done.text}"
    photo = done.json()
    assert photo["id"] == photo_id
    assert photo["status"] in ("processing", "ready"), photo["status"]
    assert photo["license"]["code"] == "CC-BY-4.0"


def test_el_objeto_esta_en_s3_con_los_bytes_exactos(
    auth_client: httpx.Client, jpeg: bytes
) -> None:
    """Lo que MinIO guardó es byte a byte lo que se subió.

    Sin esta comprobación, un presignado mal formado que devuelva 204 y guarde un
    objeto vacío pasaría el test anterior.
    """
    ticket = request_ticket(auth_client, jpeg)
    assert put_binary(ticket, jpeg).status_code in (200, 204)
    complete(auth_client, ticket["photo_id"]).raise_for_status()

    from tests.integration.s3 import get_object

    key = ticket["presigned_post"]["s3_key"]
    stored = get_object(key)
    assert stored is not None, f"MinIO no tiene el objeto {key}"
    assert sha256_hex(stored) == sha256_hex(jpeg), "los bytes guardados no son los subidos"


def test_el_presignado_rechaza_un_fichero_mas_grande_del_declarado(
    auth_client: httpx.Client, jpeg: bytes
) -> None:
    """`content-length-range` del POST presignado no es decorativo.

    Es la razón por la que `docs/api.md` eligió POST y no PUT; si S3 acepta un
    binario mayor que el declarado, la cuota del usuario deja de significar nada.
    """
    ticket = request_ticket(auth_client, jpeg)
    inflated = jpeg + b"\x00" * (10 * len(jpeg) + 8192)
    resp = put_binary(ticket, inflated)
    assert resp.status_code >= 400, (
        "MinIO aceptó un fichero mucho mayor que el declarado: "
        "el content-length-range del presignado no está haciendo su trabajo"
    )


def test_no_se_puede_completar_la_subida_de_otro(
    auth_client: httpx.Client, other_user: ApiUser, api_base: str, jpeg: bytes
) -> None:
    """La foto es de quien pidió el ticket; otro usuario no la cierra."""
    ticket = request_ticket(auth_client, jpeg)
    assert put_binary(ticket, jpeg).status_code in (200, 204)

    with httpx.Client(base_url=api_base, headers=other_user.headers, timeout=30.0) as intruder:
        resp = complete(intruder, ticket["photo_id"])
    assert resp.status_code == 404, f"se esperaba 404, llegó {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_un_error_sale_como_problem_json(auth_client: httpx.Client) -> None:
    """RFC 9457 en la ruta de error, no solo en la feliz."""
    r = auth_client.post(
        "/photos/uploads",
        json={
            "filename": "x.txt",
            "size_bytes": 10,
            "mime_type": "text/plain",
            "checksum_sha256": "0" * 64,
        },
    )
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert {"type", "title", "status"} <= set(body), body
    assert body["status"] == 422
