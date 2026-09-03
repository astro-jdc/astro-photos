"""Tests de ``app.domain.selection``: determinismo, diversidad y procedencia."""

from __future__ import annotations

import random

import pytest

from app.domain.selection import (
    FrameCandidate,
    RejectionReason,
    select_frames,
)


def frame(
    ident: str,
    quality: float,
    dx: float = 0.0,
    dy: float = 0.0,
    scale: float = 2.0,
) -> FrameCandidate:
    return FrameCandidate(
        photo_id=ident,
        quality_score=quality,
        dither_x=dx,
        dither_y=dy,
        pixel_scale_arcsec=scale,
    )


def grid(n: int, *, seed: int = 7) -> list[FrameCandidate]:
    """``n`` candidatos pseudoaleatorios pero reproducibles."""
    rng = random.Random(seed)
    return [
        frame(
            f"{i:04d}",
            round(rng.uniform(0.1, 0.99), 4),
            round(rng.random(), 4),
            round(rng.random(), 4),
            round(rng.uniform(0.8, 6.0), 3),
        )
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# Determinismo
# --------------------------------------------------------------------------- #
def test_selection_is_deterministic() -> None:
    candidates = grid(60)
    first = select_frames(candidates, 12)
    second = select_frames(candidates, 12)
    assert [f.photo_id for f in first.selected] == [f.photo_id for f in second.selected]


def test_selection_does_not_depend_on_input_order() -> None:
    """Regla dura 3: nada de depender del orden del sistema de ficheros."""
    candidates = grid(50)
    shuffled = list(candidates)
    random.Random(99).shuffle(shuffled)
    assert {f.photo_id for f in select_frames(candidates, 10).selected} == {
        f.photo_id for f in select_frames(shuffled, 10).selected
    }


def test_ties_are_broken_by_photo_id() -> None:
    """Cuatro frames idénticos salvo el id: gana el menor."""
    identical = [frame(f"id-{i}", 0.5, 0.25, 0.25, 2.0) for i in range(4)]
    result = select_frames(list(reversed(identical)), 2)
    assert next(f.photo_id for f in result.selected) == "id-0"


def test_ranks_are_consecutive_from_zero() -> None:
    result = select_frames(grid(30), 8)
    assert [f.rank for f in result.selected] == list(range(8))


# --------------------------------------------------------------------------- #
# Calidad
# --------------------------------------------------------------------------- #
def test_the_best_frame_is_always_the_reference() -> None:
    candidates = [*grid(20), frame("winner", 1.0, 0.5, 0.5, 2.0)]
    result = select_frames(candidates, 5)
    assert result.selected[0].photo_id == "winner"


def test_min_quality_rejects_with_the_documented_reason() -> None:
    candidates = [frame("a", 0.9), frame("b", 0.1, 0.5, 0.5), frame("c", 0.7, 0.2, 0.8)]
    result = select_frames(candidates, 3, min_quality=0.5)
    rejected = {r.photo_id: r.reason for r in result.rejected}
    assert rejected == {"b": RejectionReason.TOO_LOW_QUALITY}
    assert {f.photo_id for f in result.selected} == {"a", "c"}


def test_weights_are_normalised_and_proportional_to_quality() -> None:
    result = select_frames([frame("a", 0.9), frame("b", 0.3, 0.5, 0.5)], 2)
    assert sum(f.weight for f in result.selected) == pytest.approx(1.0)
    weights = {f.photo_id: f.weight for f in result.selected}
    assert weights["a"] > weights["b"]


# --------------------------------------------------------------------------- #
# Diversidad — la razón de ser del módulo
# --------------------------------------------------------------------------- #
def test_diverse_dither_beats_slightly_better_but_redundant_quality() -> None:
    """Es el caso que justifica todo el módulo.

    Tres frames con la misma fase sub-píxel y calidad 0.90, y uno con fase opuesta
    y calidad 0.80. Coger "los N mejores" elegiría los tres redundantes; para
    recuperar muestreo hay que meter el cuarto.
    """
    candidates = [
        frame("dup-1", 0.90, 0.10, 0.10),
        frame("dup-2", 0.90, 0.10, 0.10),
        frame("dup-3", 0.90, 0.10, 0.10),
        frame("diverse", 0.80, 0.60, 0.60),
    ]
    chosen = {f.photo_id for f in select_frames(candidates, 2).selected}
    assert "diverse" in chosen


def test_scale_diversity_is_also_rewarded() -> None:
    candidates = [
        frame("same-scale-1", 0.90, 0.1, 0.1, scale=2.0),
        frame("same-scale-2", 0.88, 0.1, 0.1, scale=2.0),
        frame("other-scale", 0.80, 0.1, 0.1, scale=8.0),
    ]
    chosen = {f.photo_id for f in select_frames(candidates, 2).selected}
    assert "other-scale" in chosen


def test_pure_quality_weighting_ignores_diversity() -> None:
    """Con ``quality_weight=1`` el algoritmo degenera en «los N mejores»."""
    candidates = [
        frame("dup-1", 0.90, 0.10, 0.10),
        frame("dup-2", 0.89, 0.10, 0.10),
        frame("diverse", 0.80, 0.60, 0.60),
    ]
    chosen = [f.photo_id for f in select_frames(candidates, 2, quality_weight=1.0).selected]
    assert chosen == ["dup-1", "dup-2"]


def test_phase_diversity_is_zero_for_identical_dither() -> None:
    identical = [frame(f"i{i}", 0.5 + i / 100, 0.3, 0.3) for i in range(5)]
    assert select_frames(identical, 5).phase_diversity == pytest.approx(0.0)


def test_phase_diversity_is_high_for_a_spread_grid() -> None:
    spread = [
        frame("a", 0.9, 0.0, 0.0),
        frame("b", 0.9, 0.5, 0.0),
        frame("c", 0.9, 0.0, 0.5),
        frame("d", 0.9, 0.5, 0.5),
    ]
    assert select_frames(spread, 4).phase_diversity > 0.8


def test_dither_phase_wraps_around_the_unit_torus() -> None:
    """0.99 y 0.01 son fases casi idénticas, no opuestas."""
    candidates = [
        frame("a", 0.9, 0.99, 0.99),
        frame("b", 0.9, 0.01, 0.01),
        frame("c", 0.9, 0.5, 0.5),
    ]
    result = select_frames(candidates, 2)
    assert {f.photo_id for f in result.selected} == {"a", "c"}


# --------------------------------------------------------------------------- #
# Geometría ausente
# --------------------------------------------------------------------------- #
def test_frames_without_geometry_are_rejected_when_there_are_enough_solved_ones() -> None:
    candidates = [
        *[frame(f"solved-{i}", 0.8, i / 10, i / 10) for i in range(5)],
        FrameCandidate(photo_id="unsolved", quality_score=0.99),
    ]
    result = select_frames(candidates, 3)
    reasons = {r.photo_id: r.reason for r in result.rejected}
    assert reasons["unsolved"] is RejectionReason.UNSOLVED


def test_frames_without_geometry_are_used_when_nothing_else_is_available() -> None:
    """Sin resolver aún aportan SNR; es mejor eso que no reconstruir nada."""
    candidates = [
        FrameCandidate(photo_id="a", quality_score=0.8),
        FrameCandidate(photo_id="b", quality_score=0.7),
    ]
    result = select_frames(candidates, 2)
    assert len(result.selected) == 2
    assert result.phase_diversity == 0.0


# --------------------------------------------------------------------------- #
# Bordes
# --------------------------------------------------------------------------- #
def test_asking_for_more_than_available_returns_everything() -> None:
    result = select_frames(grid(5), 50)
    assert len(result.selected) == 5
    assert result.rejected == ()


def test_empty_candidate_list_returns_an_empty_plan() -> None:
    result = select_frames([], 10)
    assert result.selected == ()
    assert result.input_count == 0


def test_target_count_must_be_positive() -> None:
    with pytest.raises(ValueError, match="target_count"):
        select_frames(grid(3), 0)


def test_quality_weight_must_be_a_fraction() -> None:
    with pytest.raises(ValueError, match="quality_weight"):
        select_frames(grid(3), 2, quality_weight=1.5)


def test_every_candidate_ends_up_either_selected_or_rejected() -> None:
    """Procedencia completa: nadie desaparece sin dejar rastro."""
    candidates = grid(40)
    result = select_frames(candidates, 9)
    accounted = {f.photo_id for f in result.selected} | {r.photo_id for r in result.rejected}
    assert accounted == {c.photo_id for c in candidates}


def test_rejected_list_is_sorted_for_stable_output() -> None:
    result = select_frames(grid(30), 5)
    ids = [r.photo_id for r in result.rejected]
    assert ids == sorted(ids)
