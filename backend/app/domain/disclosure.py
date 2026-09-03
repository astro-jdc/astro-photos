"""Etiquetado de salidas generadas. Regla dura 2 de ``CLAUDE.md``.

    Nada generado sin etiquetar. En astronomía una fuente alucinada es un falso
    descubrimiento, no un defecto estético. Toda salida de un modelo aprendido lleva
    mapa de incertidumbre y etiqueta visible.

Si eso vive solo en una convención, el día que alguien añada un pipeline se olvidará.
Aquí se convierte en una función pura que el servicio **impone**: una reconstrucción
que use un modelo aprendido y no traiga mapa de incertidumbre no se publica, se marca
``failed``.

Módulo puro: sin IO, sin base de datos.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "LEARNED_PIPELINES",
    "DisclosureViolation",
    "ResultArtifacts",
    "uses_learned_model",
    "validate_publishable",
]

#: Pipelines que ejecutan un modelo aprendido aunque no se les pase un ``model_id``
#: explícito (usan el modelo activo de su arquitectura). ``classical-stack-v1`` y
#: ``drizzle-v1`` son puramente clásicos: su salida es una combinación de píxeles
#: medidos, no una inferencia.
LEARNED_PIPELINES: frozenset[str] = frozenset({"burst-sr-v1"})


def uses_learned_model(pipeline: str, model_id: object | None = None) -> bool:
    """¿La salida de este job es, en parte, inferida por un modelo?

    Es lo que dispara el aviso de IA en el frontend. Se responde por dos vías —el
    ``model_id`` explícito y la lista de pipelines aprendidos— porque cualquiera de
    las dos sola dejaría un hueco: un pipeline aprendido sin ``model_id`` usa el
    modelo activo, y un pipeline clásico con ``model_id`` sería una incoherencia que
    conviene tratar como aprendida hasta que se demuestre lo contrario.
    """
    return model_id is not None or pipeline in LEARNED_PIPELINES


class ViolationCode(StrEnum):
    """Por qué un resultado no puede publicarse."""

    MISSING_UNCERTAINTY_MAP = "missing_uncertainty_map"
    MISSING_RESULT = "missing_result"
    MISSING_ATTRIBUTION = "missing_attribution"


@dataclass(frozen=True, slots=True)
class ResultArtifacts:
    """Lo que un pipeline dice haber dejado en S3. Sin claves = no producido."""

    pipeline: str
    model_id: str | None = None
    s3_key_result: str | None = None
    s3_key_uncertainty: str | None = None
    s3_key_weight_map: str | None = None
    s3_key_attribution: str | None = None


@dataclass(frozen=True, slots=True)
class DisclosureViolation:
    """Una razón concreta por la que el resultado se rechaza."""

    code: ViolationCode
    detail: str


def validate_publishable(artifacts: ResultArtifacts) -> tuple[DisclosureViolation, ...]:
    """Devuelve las violaciones; vacío significa "se puede publicar".

    Reglas, todas duras:

    1. No hay resultado sin fichero de resultado.
    2. **Un pipeline que usa un modelo aprendido exige mapa de incertidumbre.** Sin
       él la imagen afirma detalle que nadie puede auditar, que es exactamente el
       falso descubrimiento que la regla 2 existe para impedir. Los pipelines
       clásicos no lo exigen: su salida es combinación de píxeles medidos.
    3. Atribución siempre (regla 5 de ``docs/licensing.md``), venga de donde venga
       la salida.
    """
    violations: list[DisclosureViolation] = []

    if not artifacts.s3_key_result:
        violations.append(
            DisclosureViolation(
                ViolationCode.MISSING_RESULT,
                "El pipeline no dejó fichero de resultado.",
            )
        )

    if uses_learned_model(artifacts.pipeline, artifacts.model_id) and not (
        artifacts.s3_key_uncertainty
    ):
        violations.append(
            DisclosureViolation(
                ViolationCode.MISSING_UNCERTAINTY_MAP,
                (
                    f"El pipeline «{artifacts.pipeline}» usa un modelo aprendido y no "
                    "produjo mapa de incertidumbre. Toda salida de un modelo lleva "
                    "mapa de incertidumbre y etiqueta visible: una fuente alucinada "
                    "sin incertidumbre es un falso descubrimiento, no un defecto "
                    "estético."
                ),
            )
        )

    if not artifacts.s3_key_attribution:
        violations.append(
            DisclosureViolation(
                ViolationCode.MISSING_ATTRIBUTION,
                "El pipeline no dejó ATTRIBUTION.md, que es obligatorio siempre.",
            )
        )

    return tuple(violations)
