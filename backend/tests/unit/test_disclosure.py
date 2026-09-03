"""Regla dura 2 de ``CLAUDE.md``: nada generado sin etiquetar.

Estos tests fijan que la regla no dependa de que nadie se olvide: la impone
``validate_publishable``, y el servicio la aplica antes de publicar nada.
"""

from __future__ import annotations

import pytest

from app.domain.disclosure import (
    LEARNED_PIPELINES,
    ResultArtifacts,
    ViolationCode,
    uses_learned_model,
    validate_publishable,
)


def complete(pipeline: str = "classical-stack-v1", **kw: object) -> ResultArtifacts:
    base: dict[str, object] = {
        "pipeline": pipeline,
        "s3_key_result": "result.fits",
        "s3_key_attribution": "ATTRIBUTION.md",
    }
    base.update(kw)
    return ResultArtifacts(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ¿Usa modelo aprendido?
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pipeline", ["classical-stack-v1", "drizzle-v1"])
def test_classical_pipelines_are_not_learned(pipeline: str) -> None:
    """Su salida es combinación de píxeles medidos, no una inferencia."""
    assert uses_learned_model(pipeline) is False


@pytest.mark.parametrize("pipeline", sorted(LEARNED_PIPELINES))
def test_learned_pipelines_are_flagged_without_an_explicit_model(pipeline: str) -> None:
    """Un pipeline aprendido sin `model_id` usa el modelo activo: sigue siendo IA."""
    assert uses_learned_model(pipeline) is True


def test_an_explicit_model_makes_any_pipeline_learned() -> None:
    """Un `model_id` en un pipeline clásico es una incoherencia; se asume lo peor."""
    assert uses_learned_model("classical-stack-v1", "some-model-id") is True


def test_the_two_signals_are_independent() -> None:
    assert uses_learned_model("burst-sr-v1", None) is True
    assert uses_learned_model("drizzle-v1", None) is False


# --------------------------------------------------------------------------- #
# Publicabilidad
# --------------------------------------------------------------------------- #
def test_a_complete_classical_result_is_publishable() -> None:
    assert validate_publishable(complete()) == ()


def test_a_learned_result_needs_an_uncertainty_map() -> None:
    violations = validate_publishable(complete("burst-sr-v1"))
    assert [v.code for v in violations] == [ViolationCode.MISSING_UNCERTAINTY_MAP]


def test_a_learned_result_with_the_map_is_publishable() -> None:
    assert validate_publishable(complete("burst-sr-v1", s3_key_uncertainty="u.fits")) == ()


def test_a_classical_result_does_not_need_an_uncertainty_map() -> None:
    """No se pide por pedir: sin inferencia no hay fuente alucinada que declarar."""
    assert validate_publishable(complete("classical-stack-v1")) == ()


def test_a_result_without_a_result_file_is_refused() -> None:
    violations = validate_publishable(complete(s3_key_result=None))
    assert ViolationCode.MISSING_RESULT in {v.code for v in violations}


def test_attribution_is_required_for_every_pipeline() -> None:
    violations = validate_publishable(complete(s3_key_attribution=None))
    assert ViolationCode.MISSING_ATTRIBUTION in {v.code for v in violations}


def test_every_violation_is_reported_not_just_the_first() -> None:
    violations = validate_publishable(ResultArtifacts(pipeline="burst-sr-v1"))
    assert {v.code for v in violations} == {
        ViolationCode.MISSING_RESULT,
        ViolationCode.MISSING_UNCERTAINTY_MAP,
        ViolationCode.MISSING_ATTRIBUTION,
    }


def test_the_message_explains_why_it_matters_not_just_what_is_missing() -> None:
    """El mensaje va a un log y a un `error_message`: tiene que enseñar la regla."""
    violations = validate_publishable(complete("burst-sr-v1"))
    detail = violations[0].detail
    assert "falso descubrimiento" in detail
    assert "burst-sr-v1" in detail


def test_validation_is_pure_and_deterministic() -> None:
    artifacts = complete("burst-sr-v1")
    assert validate_publishable(artifacts) == validate_publishable(artifacts)
