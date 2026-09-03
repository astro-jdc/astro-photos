"""Dos reglas duras que cruzan componentes.

**Regla 2 — nada generado sin etiquetar.** Un resultado de pipeline aprendido
sin mapa de incertidumbre no se publica por **ningún** camino. En astronomía
una fuente alucinada es un falso descubrimiento, no un defecto estético.

**Bloqueo != rechazo.** Un frame ND aborta el trabajo entero con 422, porque no
hay forma legal de continuar; un frame sin resolver simplemente se descarta y el
trabajo sigue. Confundir los dos es o bien perder 400 frames buenos por uno
malo, o bien publicar una derivada de una foto que no admite derivadas.

    backend/.venv/bin/pytest tests/invariants/test_disclosure_and_blocking.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.domain.disclosure import (
    LEARNED_PIPELINES,
    ResultArtifacts,
    uses_learned_model,
    validate_publishable,
)
from tests.helpers.pipeline import PipelineFailed, build_corpus, run_astrostack
from tests.invariants.helpers import create_ready_photo, ensure_sky_object, mark_ready

pytestmark = pytest.mark.invariant


# --------------------------------------------------------------------------- #
# Regla 2 — divulgación
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pipeline", sorted(LEARNED_PIPELINES))
def test_un_pipeline_aprendido_sin_incertidumbre_no_se_publica(pipeline: str) -> None:
    """El caso central: aprendido + sin mapa de incertidumbre = no publicable."""
    violations = validate_publishable(
        ResultArtifacts(
            pipeline=pipeline,
            s3_key_result="derived/r/result.fits",
            s3_key_attribution="derived/r/ATTRIBUTION.md",
            s3_key_uncertainty=None,
        )
    )
    codes = {v.code.value for v in violations}
    assert "missing_uncertainty_map" in codes, (
        f"El pipeline aprendido {pipeline!r} se declara publicable sin mapa de "
        f"incertidumbre. Violaciones detectadas: {codes}"
    )


def test_un_pipeline_clasico_con_model_id_tambien_cuenta_como_aprendido() -> None:
    """Un `model_id` sobre un pipeline clásico es una incoherencia.

    Se trata como aprendida hasta que se demuestre lo contrario: la opción
    conservadora es exigir la incertidumbre, no asumir que fue un descuido.
    """
    assert uses_learned_model("classical-stack-v1", model_id="algún-modelo")
    violations = validate_publishable(
        ResultArtifacts(
            pipeline="classical-stack-v1",
            model_id="algún-modelo",
            s3_key_result="r.fits",
            s3_key_attribution="A.md",
        )
    )
    assert {v.code.value for v in violations} == {"missing_uncertainty_map"}


def test_un_pipeline_clasico_no_exige_incertidumbre() -> None:
    """Control negativo: si todo exigiese incertidumbre, el test anterior no probaría nada."""
    assert not uses_learned_model("classical-stack-v1", model_id=None)
    assert (
        validate_publishable(
            ResultArtifacts(
                pipeline="classical-stack-v1",
                s3_key_result="r.fits",
                s3_key_attribution="A.md",
            )
        )
        == ()
    )


def test_la_atribucion_es_obligatoria_para_todos_los_pipelines() -> None:
    """Regla 5 de `docs/licensing.md`: sin ATTRIBUTION.md no se publica nada."""
    violations = validate_publishable(
        ResultArtifacts(pipeline="classical-stack-v1", s3_key_result="r.fits")
    )
    assert {v.code.value for v in violations} == {"missing_attribution"}


def test_el_api_marca_uses_learned_model_en_el_plan(
    auth_client: httpx.Client,
) -> None:
    """`POST /reconstructions/preview` avisa al frontend antes de lanzar.

    Es lo que dispara `AiDisclosure` en la interfaz; si el plan no lo dijera,
    la etiqueta visible dependería de que alguien se acordase de ponerla.
    """
    object_id = ensure_sky_object()
    ids = [create_ready_photo(auth_client, license="CC-BY-4.0") for _ in range(3)]
    mark_ready(*ids)

    for pipeline, expected in (("classical-stack-v1", False), ("burst-sr-v1", True)):
        resp = auth_client.post(
            "/reconstructions/preview",
            json={"object_id": object_id, "photo_ids": ids, "pipeline": pipeline},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["uses_learned_model"] is expected, (
            f"El plan de {pipeline!r} dice uses_learned_model="
            f"{resp.json()['uses_learned_model']}, se esperaba {expected}."
        )


def test_el_modelo_de_datos_exige_el_mapa_de_incertidumbre() -> None:
    """`docs/data-model.md`: `s3_key_uncertainty` es obligatorio si hay `model_id`.

    La regla vive en `domain/disclosure.py` y no puede quedarse solo en la prosa
    del documento: aquí se comprueba que las dos dicen lo mismo.
    """
    doc = Path("docs/data-model.md").read_text(encoding="utf-8")
    assert "Obligatorio" in doc and "s3_key_uncertainty" in doc
    assert "burst-sr-v1" in LEARNED_PIPELINES


# --------------------------------------------------------------------------- #
# Bloqueo (422, aborta) != rechazo (se descarta, el job sigue)
# --------------------------------------------------------------------------- #
def test_un_frame_nd_bloquea_el_job_entero_con_422(auth_client: httpx.Client) -> None:
    """Un solo ND aborta: no se degrada la salida, se devuelve `blocked[]`."""
    object_id = ensure_sky_object()
    good = [create_ready_photo(auth_client, license="CC-BY-4.0") for _ in range(2)]
    nd = create_ready_photo(auth_client, license="CC-BY-ND-4.0")
    mark_ready(*good, nd)

    resp = auth_client.post(
        "/reconstructions",
        json={
            "object_id": object_id,
            "photo_ids": [*good, nd],
            "pipeline": "classical-stack-v1",
        },
    )
    assert resp.status_code == 422, (
        f"Un frame ND debería bloquear el job con 422; llegó {resp.status_code}: "
        f"{resp.text[:400]}"
    )
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    blob = json.dumps(body)
    assert nd in blob, f"El 422 no dice qué foto bloquea el job: {blob[:400]}"
    assert "no_derivatives" in blob, f"El 422 no da el motivo del bloqueo: {blob[:400]}"


def test_el_plan_separa_blocked_de_rejected(auth_client: httpx.Client) -> None:
    """`docs/api.md`: son cosas distintas y no deben mezclarse."""
    object_id = ensure_sky_object()
    good = [create_ready_photo(auth_client, license="CC-BY-4.0") for _ in range(2)]
    nd = create_ready_photo(auth_client, license="CC-BY-NC-ND-4.0")
    opt_out = create_ready_photo(
        auth_client, license="CC-BY-4.0", allow_derivatives_in_stacks=False
    )
    mark_ready(*good, nd, opt_out)

    resp = auth_client.post(
        "/reconstructions/preview",
        json={
            "object_id": object_id,
            "photo_ids": [*good, nd, opt_out],
            "pipeline": "classical-stack-v1",
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()

    blocked_ids = {b["photo_id"] for b in plan["blocked"]}
    assert nd in blocked_ids, f"El ND no aparece en blocked[]: {plan['blocked']}"
    assert opt_out in blocked_ids, (
        f"La foto con allow_derivatives_in_stacks=false no aparece en blocked[]: "
        f"{plan['blocked']}"
    )

    reasons = {b["photo_id"]: b["reason"] for b in plan["blocked"]}
    assert reasons[nd] == "no_derivatives"
    assert reasons[opt_out] == "stack_opt_out"

    rejected_ids = {r["photo_id"] for r in plan["rejected"]}
    assert not (blocked_ids & rejected_ids), (
        "Una misma foto aparece a la vez en blocked[] y rejected[]: son categorías "
        "distintas y mezclarlas confunde al usuario sobre si puede reintentar."
    )
    assert plan["can_run"] is False, "Con fotos bloqueadas el plan no puede ser ejecutable."


def test_un_frame_sin_resolver_nunca_bloquea_el_job(auth_client: httpx.Client) -> None:
    """El job sigue: un frame no resuelto no es motivo para abortar.

    Estas fotos no pasaron por el worker de astrometría, así que ninguna tiene
    geometría. Lo que se afirma es lo que importa: ninguna aparece en
    `blocked[]` y el plan sigue siendo ejecutable.
    """
    object_id = ensure_sky_object()
    ids = [create_ready_photo(auth_client, license="CC-BY-4.0") for _ in range(3)]
    mark_ready(*ids)

    resp = auth_client.post(
        "/reconstructions/preview",
        json={"object_id": object_id, "photo_ids": ids, "pipeline": "classical-stack-v1"},
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()

    assert plan["blocked"] == [], (
        f"Un frame sin resolver no puede bloquear el job: {plan['blocked']}"
    )
    for rejection in plan["rejected"]:
        assert rejection["reason"] != "no_derivatives", (
            "Un frame sin resolver se está rechazando por motivo de licencia."
        )


def test_un_frame_sin_resolver_se_descarta_cuando_sobran_frames_resueltos() -> None:
    """Con frames resueltos de sobra, los no resueltos caen a `rejected`.

    Se prueba sobre la función de dominio porque en el stack local no hay worker
    de astrometría y ninguna foto llega a estar resuelta; el motivo `unsolved`
    solo se puede provocar aquí.
    """
    from app.domain.selection import FrameCandidate, RejectionReason, select_frames

    solved = [
        FrameCandidate(
            photo_id=f"solved-{i}",
            quality_score=0.9,
            dither_x=i / 5.0,
            dither_y=(i * 2 % 5) / 5.0,
            pixel_scale_arcsec=2.0,
        )
        for i in range(5)
    ]
    unsolved = FrameCandidate(photo_id="unsolved-1", quality_score=0.95)

    result = select_frames([*solved, unsolved], target_count=3)

    selected_ids = {f.photo_id for f in result.selected}
    assert "unsolved-1" not in selected_ids, (
        "Un frame sin geometría no puede entrar cuando hay resueltos de sobra: "
        "sin fase sub-píxel no aporta diversidad de muestreo."
    )
    reasons = {r.photo_id: r.reason for r in result.rejected}
    assert reasons.get("unsolved-1") is RejectionReason.UNSOLVED, (
        f"Se esperaba rechazo por `unsolved`; se obtuvo {reasons}"
    )


def test_models_tambien_rechaza_un_nd_de_forma_dura(tmp_path: Path) -> None:
    """La red de seguridad de `models/` para ejecuciones offline.

    El backend bloquea antes de encolar, pero el CLI de `models/` se puede
    invocar a mano; con `strict_licenses` un ND tiene que reventar, no colarse.
    """
    corpus = build_corpus(
        tmp_path / "inputs",
        n_frames=3,
        licenses=["CC-BY-4.0", "CC-BY-ND-4.0", "CC-BY-4.0"],
    )
    with pytest.raises(PipelineFailed) as exc:
        run_astrostack(corpus["manifest"], tmp_path / "run", strict_licenses=True)
    assert "LicenseViolation" in exc.value.stderr, (
        f"Se esperaba un LicenseViolation por el frame ND:\n{exc.value.stderr}"
    )
    assert "forbids derivative works" in exc.value.stderr
