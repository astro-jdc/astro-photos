"""Lambda de verificación de subidas.

Se dispara con `s3:ObjectCreated` sobre el prefijo `staging/` del bucket de
uploads (entregado vía EventBridge, ver `compute_stack.py`). Comprueba lo barato
y decisivo antes de gastar un worker de ingesta en el fichero:

1. Tamaño dentro de límites (ni 0 bytes ni por encima de `MAX_UPLOAD_BYTES`).
2. *Magic bytes* coherentes con un formato que sabemos leer.
3. La clave sigue el patrón `staging/<user_id>/<photo_id>` que emite el backend.

Si pasa, encola el trabajo de ingesta. Si no, publica un mensaje de cuarentena en
la misma cola con `action=quarantine`: el backend pone `status='quarantined'`
(`docs/data-model.md`). Nunca se borra el objeto aquí — el ciclo de vida del
bucket ya expira `staging/` solo.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
sqs = boto3.client("sqs")

INGEST_QUEUE_URL = os.environ["INGEST_QUEUE_URL"]
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
ENVIRONMENT = os.environ["ENVIRONMENT"]

#: Firmas de los formatos que `models/astrostack/io` sabe abrir. Un EXIF miente
#: fácil; los primeros bytes del fichero no.
MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"II*\x00": "image/tiff",  # little endian: TIFF, CR2, NEF, ARW, DNG
    b"MM\x00*": "image/tiff",  # big endian
    b"SIMPLE  =": "image/fits",
    b"RIFF": "image/webp",
    b"\x00\x00\x00\x0cjP": "image/jp2",
    b"FUJIFILM": "image/x-fuji-raf",
    b"II\x1a\x00\x00\x00HEAPCCDR": "image/x-canon-crw",
}
MAX_MAGIC = max(len(k) for k in MAGIC)


def _sniff(head: bytes) -> str | None:
    for signature, mime in MAGIC.items():
        if head.startswith(signature):
            return mime
    # CR3 / HEIF: caja `ftyp` en el offset 4.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "image/x-canon-cr3"
    return None


def _publish(bucket: str, key: str, size: int, mime: str | None, reason: str | None) -> None:
    body = {
        "action": "quarantine" if reason else "ingest",
        "bucket": bucket,
        "key": key,
        "size_bytes": size,
        "detected_mime": mime,
        "reason": reason,
        "environment": ENVIRONMENT,
    }
    sqs.send_message(QueueUrl=INGEST_QUEUE_URL, MessageBody=json.dumps(body))
    LOG.info(json.dumps({"event": "upload_verified", **body}))


def _check(bucket: str, key: str, size: int) -> tuple[str | None, str | None]:
    """Devuelve `(mime, motivo_de_rechazo)`."""
    if size == 0:
        return None, "objeto vacio"
    if size > MAX_UPLOAD_BYTES:
        return None, f"supera el limite de {MAX_UPLOAD_BYTES} bytes"

    parts = key.split("/")
    if len(parts) < 3 or parts[0] != "staging":
        return None, f"clave fuera del patron staging/<user>/<photo>: {key}"

    head = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{MAX_MAGIC + 16}")["Body"].read()
    mime = _sniff(head)
    if mime is None:
        return None, "formato no reconocido por los magic bytes"
    return mime, None


def handler(event: dict, context: object) -> dict:
    """Evento de EventBridge `aws.s3 / Object Created`."""
    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    key = detail.get("object", {}).get("key")
    size = int(detail.get("object", {}).get("size", 0))
    if not bucket or not key:
        LOG.warning(json.dumps({"event": "evento_ignorado", "raw": event}))
        return {"ok": False}

    mime, reason = _check(bucket, key, size)
    _publish(bucket, key, size, mime, reason)
    return {"ok": reason is None, "mime": mime, "reason": reason}
