"""Lambda dispatcher: cola `reconstruct` → job de AWS Batch.

Deliberadamente tonta. Toda la decisión (qué fotos entran, con qué pesos, qué
licencia sale) ya la tomó el backend y viaja en el mensaje; aquí solo se traduce
un mensaje de SQS en un `SubmitJob`. Si esto tuviera lógica de negocio habría dos
sitios donde vive la misma regla, y uno de los dos se quedaría atrás.

Mensaje esperado (`docs/api.md`, `POST /reconstructions`)::

    {
      "reconstruction_id": "uuid",
      "pipeline": "classical-stack-v1",
      "pipeline_version": "sha",
      "params": {...},
      "input_keys": ["originals/....fits", ...],
      "output_prefix": "reconstructions/<uuid>/"
    }

Errores: se dejan propagar. La cola tiene DLQ (`maxReceiveCount=3`) y la
profundidad de la DLQ está alarmada en el stack de observabilidad.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

batch = boto3.client("batch")

JOB_QUEUE = os.environ["BATCH_JOB_QUEUE"]
JOB_DEFINITION = os.environ["BATCH_JOB_DEFINITION"]
ENVIRONMENT = os.environ["ENVIRONMENT"]
DERIVED_BUCKET = os.environ["DERIVED_BUCKET"]
ORIGINALS_BUCKET = os.environ["ORIGINALS_BUCKET"]

REQUIRED_FIELDS = ("reconstruction_id", "pipeline")


def _submit(message: dict) -> str:
    missing = [f for f in REQUIRED_FIELDS if not message.get(f)]
    if missing:
        raise ValueError(f"mensaje incompleto, faltan {missing}")

    reconstruction_id = str(message["reconstruction_id"])
    # El nombre del job es idempotente por reconstrucción: si SQS reentrega el
    # mismo mensaje, en la consola de Batch se ve claramente que es el mismo trabajo.
    job_name = f"recon-{reconstruction_id}".replace("_", "-")[:128]

    response = batch.submit_job(
        jobName=job_name,
        jobQueue=JOB_QUEUE,
        jobDefinition=JOB_DEFINITION,
        containerOverrides={
            "environment": [
                {"name": "RECONSTRUCTION_ID", "value": reconstruction_id},
                {"name": "PIPELINE", "value": str(message["pipeline"])},
                {"name": "PIPELINE_VERSION", "value": str(message.get("pipeline_version", ""))},
                {"name": "PARAMS_JSON", "value": json.dumps(message.get("params", {}))},
                {"name": "INPUT_KEYS_JSON", "value": json.dumps(message.get("input_keys", []))},
                {"name": "OUTPUT_PREFIX", "value": str(message.get("output_prefix", ""))},
                {"name": "ORIGINALS_BUCKET", "value": ORIGINALS_BUCKET},
                {"name": "DERIVED_BUCKET", "value": DERIVED_BUCKET},
                {"name": "ENVIRONMENT", "value": ENVIRONMENT},
            ]
        },
        tags={
            "Project": "astro-photos",
            "Environment": ENVIRONMENT,
            "ReconstructionId": reconstruction_id,
        },
        propagateTags=True,
    )
    job_id = response["jobId"]
    LOG.info(
        json.dumps(
            {
                "event": "batch_job_submitted",
                "reconstruction_id": reconstruction_id,
                "batch_job_id": job_id,
                "pipeline": message["pipeline"],
            }
        )
    )
    return job_id


def handler(event: dict, context: object) -> dict:
    """Entrada de SQS. `batch_size` es 1, pero se itera por si se sube."""
    submitted = []
    for record in event.get("Records", []):
        message = json.loads(record["body"])
        submitted.append(_submit(message))
    return {"submitted": submitted}
