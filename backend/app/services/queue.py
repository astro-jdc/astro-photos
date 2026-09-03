"""SQS (ElasticMQ en local). Encolado con deduplicación por ``Idempotency-Key``."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, get_settings
from app.core.errors import UpstreamError

__all__ = ["QueueMessage", "QueueService"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QueueMessage:
    """Un mensaje recibido del consumidor."""

    message_id: str
    receipt_handle: str
    body: dict[str, Any]
    attributes: dict[str, str]


class QueueService:
    """Fachada sobre SQS. boto3 es síncrono ⇒ todo va por ``asyncio.to_thread``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._client: Any = boto3.client(
            "sqs",
            endpoint_url=self._cfg.sqs_endpoint_url,
            region_name=self._cfg.s3_region,
            aws_access_key_id=self._cfg.aws_access_key_id,
            aws_secret_access_key=self._cfg.aws_secret_access_key,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
        self._urls: dict[str, str] = {}

    async def queue_url(self, name: str) -> str:
        cached = self._urls.get(name)
        if cached:
            return cached
        try:
            resp = await asyncio.to_thread(self._client.get_queue_url, QueueName=name)
        except (BotoCoreError, ClientError) as exc:
            log.error("queue_url_failed", queue=name, error=str(exc))
            raise UpstreamError(f"La cola «{name}» no está disponible.") from exc
        url = str(resp["QueueUrl"])
        self._urls[name] = url
        return url

    async def send(
        self,
        queue_name: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        delay_seconds: int = 0,
    ) -> str:
        """Encola un trabajo.

        ``idempotency_key`` viaja como atributo de mensaje **y** como
        ``MessageDeduplicationId`` en colas FIFO: la garantía real de "una sola vez"
        la da la fila de base de datos (``UNIQUE (requested_by, idempotency_key)``),
        pero repetir aquí evita un job duplicado en el reintento del cliente.
        """
        url = await self.queue_url(queue_name)
        attributes: dict[str, Any] = {}
        if idempotency_key:
            attributes["IdempotencyKey"] = {
                "StringValue": idempotency_key,
                "DataType": "String",
            }
        params: dict[str, Any] = {
            "QueueUrl": url,
            "MessageBody": json.dumps(body, default=str),
            "DelaySeconds": delay_seconds,
        }
        if attributes:
            params["MessageAttributes"] = attributes
        if url.endswith(".fifo") and idempotency_key:
            params["MessageDeduplicationId"] = idempotency_key
            params["MessageGroupId"] = str(body.get("group", "default"))
        try:
            resp = await asyncio.to_thread(self._client.send_message, **params)
        except (BotoCoreError, ClientError) as exc:
            log.error("enqueue_failed", queue=queue_name, error=str(exc))
            raise UpstreamError("No se pudo encolar el trabajo.") from exc
        return str(resp["MessageId"])

    async def receive(
        self, queue_name: str, *, max_messages: int = 10, wait_seconds: int = 20
    ) -> list[QueueMessage]:
        """Long polling. Lo usa el worker, nunca un handler HTTP."""
        url = await self.queue_url(queue_name)
        resp = await asyncio.to_thread(
            self._client.receive_message,
            QueueUrl=url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            MessageAttributeNames=["All"],
        )
        out: list[QueueMessage] = []
        for raw in resp.get("Messages", []):
            try:
                body = json.loads(raw["Body"])
            except json.JSONDecodeError:
                log.warning("bad_message_body", message_id=raw.get("MessageId"))
                body = {}
            out.append(
                QueueMessage(
                    message_id=str(raw["MessageId"]),
                    receipt_handle=str(raw["ReceiptHandle"]),
                    body=body if isinstance(body, dict) else {},
                    attributes={
                        k: str(v.get("StringValue", ""))
                        for k, v in (raw.get("MessageAttributes") or {}).items()
                    },
                )
            )
        return out

    async def delete(self, queue_name: str, receipt_handle: str) -> None:
        url = await self.queue_url(queue_name)
        await asyncio.to_thread(
            self._client.delete_message, QueueUrl=url, ReceiptHandle=receipt_handle
        )

    async def healthy(self) -> bool:
        """``/readyz``: la cola de ingesta existe y responde."""
        try:
            await self.queue_url(self._cfg.sqs_queue_ingest)
        except UpstreamError:
            return False
        return True
