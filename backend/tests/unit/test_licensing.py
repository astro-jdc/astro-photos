"""Test de tabla de ``app.domain.licensing``. **El test más importante del repo.**

Cubre las 8 licencias del catálogo en todas las combinaciones relevantes:

* las 8 individuales,
* los 64 pares ordenados,
* los 125 tríos sobre las 5 licencias que permiten derivadas,
* todos los casos de bloqueo duro (ND, ARR, opt-out de stacks),
* el congelado de licencia en las 64 transiciones posibles.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from app.domain.licensing import (
    LICENSE_CATALOG,
    BlockReason,
    LicenseCode,
    PhotoLicenseFacts,
    can_change_license,
    enforce_stack_consent,
    license_info,
    most_restrictive,
    resolve_output_license,
)

C = LicenseCode

#: Las 5 que pueden entrar en una reconstrucción.
ALLOWED = (C.CC0, C.CC_BY, C.CC_BY_SA, C.CC_BY_NC, C.CC_BY_NC_SA)
#: Las 3 que bloquean el job.
BLOCKING = (C.CC_BY_ND, C.CC_BY_NC_ND, C.ARR)

LOCKED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def facts(*codes: LicenseCode, stacks: bool = True) -> list[PhotoLicenseFacts]:
    return [
        PhotoLicenseFacts(
            photo_id=f"00000000-0000-0000-0000-{index:012d}",
            license=code,
            allow_derivatives_in_stacks=stacks,
        )
        for index, code in enumerate(codes)
    ]


# --------------------------------------------------------------------------- #
# Catálogo
# --------------------------------------------------------------------------- #
def test_catalog_has_exactly_the_eight_licenses_of_the_doc() -> None:
    assert len(LICENSE_CATALOG) == 8
    assert {i.code for i in LICENSE_CATALOG} == set(LicenseCode)


@pytest.mark.parametrize(
    ("code", "commercial", "derivatives", "sharealike", "restrictiveness"),
    [
        (C.CC0, True, True, False, 0),
        (C.CC_BY, True, True, False, 1),
        (C.CC_BY_SA, True, True, True, 2),
        (C.CC_BY_NC, False, True, False, 3),
        (C.CC_BY_NC_SA, False, True, True, 4),
        (C.CC_BY_ND, True, False, False, 5),
        (C.CC_BY_NC_ND, False, False, False, 6),
        (C.ARR, False, False, False, 7),
    ],
)
def test_catalog_flags_match_the_documented_table(
    code: LicenseCode,
    commercial: bool,
    derivatives: bool,
    sharealike: bool,
    restrictiveness: int,
) -> None:
    """La tabla de ``docs/licensing.md``, verbatim."""
    info = license_info(code)
    assert info.allows_commercial is commercial
    assert info.allows_derivatives is derivatives
    assert info.requires_sharealike is sharealike
    assert info.restrictiveness == restrictiveness


def test_restrictiveness_is_a_total_order_without_ties() -> None:
    values = [i.restrictiveness for i in LICENSE_CATALOG]
    assert sorted(values) == list(range(8))


def test_only_cc0_waives_attribution_but_the_resolver_still_requires_it() -> None:
    """CC0 no exige atribución legalmente; el producto la emite igual (regla 5)."""
    assert license_info(C.CC0).requires_attribution is False
    assert resolve_output_license(facts(C.CC0, C.CC0)).requires_attribution is True


# --------------------------------------------------------------------------- #
# Regla 1 — bloqueo duro
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", BLOCKING)
def test_no_derivatives_licenses_block_the_job(code: LicenseCode) -> None:
    result = resolve_output_license(facts(C.CC_BY, code))
    assert not result.ok
    assert result.resulting_license is None
    assert len(result.blocked) == 1
    assert result.blocked[0].reason is BlockReason.NO_DERIVATIVES
    assert result.blocked[0].license is code


@pytest.mark.parametrize("code", ALLOWED)
def test_stack_opt_out_blocks_even_a_permissive_license(code: LicenseCode) -> None:
    """El consentimiento es independiente de la licencia (``docs/licensing.md``)."""
    result = resolve_output_license(facts(code, stacks=False))
    assert not result.ok
    assert result.blocked[0].reason is BlockReason.STACK_OPT_OUT


def test_the_job_is_never_degraded_only_rejected() -> None:
    """Regla 1: no se quitan fotos por nuestra cuenta para salvar el job."""
    result = resolve_output_license(facts(C.CC0, C.CC_BY, C.ARR))
    assert result.resulting_license is None
    assert [b.photo_id for b in result.blocked] == ["00000000-0000-0000-0000-000000000002"]
    # …pero sí se dice cuáles sobrevivirían, para que el usuario pueda reintentar.
    assert len(result.accepted_photo_ids) == 2


def test_a_blocked_photo_reports_the_license_reason_before_the_consent_one() -> None:
    """Con ND *y* opt-out gana el motivo legal: es el que el usuario no puede cambiar."""
    result = resolve_output_license(facts(C.CC_BY_ND, stacks=False))
    assert result.blocked[0].reason is BlockReason.NO_DERIVATIVES


def test_all_blocking_photos_are_reported_not_just_the_first() -> None:
    result = resolve_output_license(facts(C.CC_BY, C.ARR, C.CC_BY_ND, C.CC_BY_NC_ND))
    assert len(result.blocked) == 3


# --------------------------------------------------------------------------- #
# Reglas 2-4 — combinación
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ALLOWED)
def test_single_allowed_input_keeps_its_own_license(code: LicenseCode) -> None:
    assert resolve_output_license(facts(code)).resulting_license is code


def _expected(codes: tuple[LicenseCode, ...]) -> LicenseCode:
    """Las reglas 2-4 escritas de forma independiente de la implementación."""
    infos = [license_info(c) for c in codes]
    nc = any(not i.allows_commercial for i in infos)
    sa = any(i.requires_sharealike for i in infos)
    if nc and sa:
        return C.CC_BY_NC_SA
    if nc:
        return C.CC_BY_NC
    if sa:
        return C.CC_BY_SA
    return C.CC0 if all(c is C.CC0 for c in codes) else C.CC_BY


@pytest.mark.parametrize("pair", list(itertools.product(ALLOWED, repeat=2)))
def test_all_64_ordered_pairs_of_combinable_licenses(
    pair: tuple[LicenseCode, LicenseCode],
) -> None:
    result = resolve_output_license(facts(*pair))
    assert result.ok
    assert result.resulting_license is _expected(pair)


@pytest.mark.parametrize("triple", list(itertools.product(ALLOWED, repeat=3)))
def test_all_125_triples_of_combinable_licenses(
    triple: tuple[LicenseCode, LicenseCode, LicenseCode],
) -> None:
    result = resolve_output_license(facts(*triple))
    assert result.ok
    assert result.resulting_license is _expected(triple)


@pytest.mark.parametrize("pair", list(itertools.product(ALLOWED, repeat=2)))
def test_combination_is_order_independent(pair: tuple[LicenseCode, LicenseCode]) -> None:
    a, b = pair
    assert (
        resolve_output_license(facts(a, b)).resulting_license
        is resolve_output_license(facts(b, a)).resulting_license
    )


@pytest.mark.parametrize("pair", list(itertools.product(ALLOWED, repeat=2)))
def test_combination_is_idempotent(pair: tuple[LicenseCode, LicenseCode]) -> None:
    """Añadir una copia de una entrada no cambia el resultado."""
    a, b = pair
    assert (
        resolve_output_license(facts(a, b)).resulting_license
        is resolve_output_license(facts(a, b, a)).resulting_license
    )


def test_cc0_only_survives_unanimity() -> None:
    assert resolve_output_license(facts(C.CC0, C.CC0, C.CC0)).resulting_license is C.CC0
    assert resolve_output_license(facts(C.CC0, C.CC_BY)).resulting_license is C.CC_BY


@pytest.mark.parametrize("other", ALLOWED)
def test_nc_is_contagious(other: LicenseCode) -> None:
    result = resolve_output_license(facts(C.CC_BY_NC, other))
    assert result.resulting_license is not None
    assert license_info(result.resulting_license).allows_commercial is False


@pytest.mark.parametrize("other", ALLOWED)
def test_sa_is_contagious(other: LicenseCode) -> None:
    result = resolve_output_license(facts(C.CC_BY_SA, other))
    assert result.resulting_license is not None
    assert license_info(result.resulting_license).requires_sharealike is True


def test_nc_and_sa_together_give_nc_sa() -> None:
    assert resolve_output_license(facts(C.CC_BY_NC, C.CC_BY_SA)).resulting_license is C.CC_BY_NC_SA


@pytest.mark.parametrize("pair", list(itertools.product(ALLOWED, repeat=2)))
def test_output_is_never_more_permissive_than_the_most_restrictive_input(
    pair: tuple[LicenseCode, LicenseCode],
) -> None:
    """La salida nunca puede permitir algo que una entrada prohibía."""
    result = resolve_output_license(facts(*pair))
    assert result.resulting_license is not None
    out = license_info(result.resulting_license)
    for code in pair:
        info = license_info(code)
        if not info.allows_commercial:
            assert not out.allows_commercial
        if info.requires_sharealike:
            assert out.requires_sharealike


def test_output_is_always_one_of_the_four_combinable_outputs() -> None:
    """Nunca sale un ND ni un ARR de una combinación: serían inconsistentes."""
    outputs = set()
    for size in (1, 2, 3):
        for combo in itertools.product(ALLOWED, repeat=size):
            result = resolve_output_license(facts(*combo))
            assert result.resulting_license is not None
            outputs.add(result.resulting_license)
    assert outputs <= {C.CC0, C.CC_BY, C.CC_BY_SA, C.CC_BY_NC, C.CC_BY_NC_SA}


def test_empty_input_resolves_to_nothing_without_crashing() -> None:
    result = resolve_output_license([])
    assert result.resulting_license is None
    assert result.blocked == ()
    assert not result.ok


def test_many_inputs_still_resolve_deterministically() -> None:
    """500 entradas, el tope del producto: el resultado no depende del tamaño."""
    codes = [ALLOWED[i % len(ALLOWED)] for i in range(500)]
    first = resolve_output_license(facts(*codes))
    second = resolve_output_license(facts(*codes))
    assert first.resulting_license is second.resulting_license is C.CC_BY_NC_SA


# --------------------------------------------------------------------------- #
# Coherencia ND ⇒ sin derivadas en stacks
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", BLOCKING)
def test_nd_forces_the_stack_opt_out(code: LicenseCode) -> None:
    assert enforce_stack_consent(code, True) is False


@pytest.mark.parametrize("code", ALLOWED)
@pytest.mark.parametrize("declared", [True, False])
def test_permissive_licenses_respect_the_declared_consent(
    code: LicenseCode, declared: bool
) -> None:
    assert enforce_stack_consent(code, declared) is declared


# --------------------------------------------------------------------------- #
# Congelado de licencia
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pair", list(itertools.product(list(C), repeat=2)))
def test_unlocked_license_can_change_to_anything(
    pair: tuple[LicenseCode, LicenseCode],
) -> None:
    current, new = pair
    assert can_change_license(current, new, None).allowed is True


@pytest.mark.parametrize("pair", list(itertools.product(list(C), repeat=2)))
def test_locked_license_can_only_be_relaxed(
    pair: tuple[LicenseCode, LicenseCode],
) -> None:
    current, new = pair
    decision = can_change_license(current, new, LOCKED_AT)
    expected = license_info(new).restrictiveness <= license_info(current).restrictiveness
    assert decision.allowed is expected


def test_locked_license_rejection_explains_why() -> None:
    decision = can_change_license(C.CC_BY, C.ARR, LOCKED_AT)
    assert decision.allowed is False
    assert "2026-01-01" in decision.reason
    assert "ARR" in decision.reason and "CC-BY-4.0" in decision.reason


def test_a_no_op_change_is_always_allowed_even_when_locked() -> None:
    for code in C:
        assert can_change_license(code, code, LOCKED_AT).allowed is True


# --------------------------------------------------------------------------- #
# Utilidad de presentación
# --------------------------------------------------------------------------- #
def test_most_restrictive_is_not_the_combination_rule() -> None:
    """Documenta la diferencia: el máximo simple daría ND, que es inaceptable."""
    assert most_restrictive([C.CC_BY, C.CC_BY_ND]) is C.CC_BY_ND
    assert resolve_output_license(facts(C.CC_BY, C.CC_BY_ND)).resulting_license is None


def test_most_restrictive_of_nothing_is_none() -> None:
    assert most_restrictive([]) is None
