"""Position weighting and arbitration (scope 8.2, 8.4).

Hand-built `ScorerResult`s, no router, no store. Arbitration semantics are where this
design is most likely to be wrong, and fake inputs are the cheapest way to find out.
"""

from __future__ import annotations

import pytest

from longrun.core.geo.segments import segment_route
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, Tier
from longrun.core.plan.arbitrate import (
    TRADEOFF_MARGIN,
    compare,
    rank_flags,
    residual_flags,
    score_candidate,
)
from longrun.core.plan.weighting import (
    FLAT_FRACTION,
    HEAT_MAX_WEIGHT,
    MAX_WEIGHT,
    position_weight,
    weight_for,
    weighted_severity,
)


def _route(n: int = 101, spacing_m: float = 100.0) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.0009, lon=-122.4, cum_dist_m=i * spacing_m)
            for i in range(n)
        ],
    )


ROUTE = _route()
SEGMENTS = segment_route(ROUTE, max_len_m=1000.0)
TOTAL_M = ROUTE.length_m


def _flag(
    scorer: str,
    segment_id: str,
    tier: Tier,
    kind: FlagKind = FlagKind.SOFT,
    severity: float = 0.5,
) -> Flag:
    return Flag(
        scorer=scorer,
        segment_id=segment_id,
        kind=kind,
        tier=tier,
        severity=severity,
        reason_code="rc",
    )


# --- w(d) shape (scope 8.2) -------------------------------------------------


def test_weight_is_flat_through_the_first_forty_percent() -> None:
    assert position_weight(0.0) == 1.0
    assert position_weight(0.2) == 1.0
    assert position_weight(FLAT_FRACTION) == 1.0


def test_weight_reaches_the_ceiling_at_the_finish() -> None:
    assert position_weight(1.0) == pytest.approx(MAX_WEIGHT)


def test_weight_rises_linearly_after_the_knee() -> None:
    assert position_weight(0.7) == pytest.approx(1.5)


def test_weight_is_continuous_so_the_optimizer_cannot_game_a_step() -> None:
    """Scope 8.2: a stepped weight invites shoving a hostile segment before the step."""
    previous = position_weight(0.0)
    for i in range(1, 1001):
        current = position_weight(i / 1000)
        assert current - previous < 0.01
        previous = current


def test_weight_is_monotonic() -> None:
    values = [position_weight(i / 100) for i in range(101)]
    assert values == sorted(values)


def test_fraction_is_clamped() -> None:
    assert position_weight(-0.5) == 1.0
    assert position_weight(1.5) == pytest.approx(MAX_WEIGHT)


def test_heat_uses_a_lower_ceiling_than_hostility() -> None:
    """The ETA vector already carries time of day; the full ramp would double-count."""
    assert weight_for("heat_stress", 1.0) == pytest.approx(HEAT_MAX_WEIGHT)
    assert weight_for("segment_hostility", 1.0) == pytest.approx(MAX_WEIGHT)


def test_legality_is_never_position_weighted() -> None:
    """An illegal segment is illegal at km 2 and at km 80 (scope 8.2)."""
    assert weight_for("legality", 0.0) == 1.0
    assert weight_for("legality", 1.0) == 1.0
    assert weight_for("hazards", 1.0) == 1.0


def test_unknown_scorer_is_not_weighted() -> None:
    assert weight_for("something_new", 1.0) == 1.0


def test_weighted_severity_grows_with_position() -> None:
    flag = _flag("segment_hostility", "s0", Tier.COMFORT, severity=0.5)
    assert weighted_severity(flag, 0.0) == pytest.approx(0.5)
    assert weighted_severity(flag, 1.0) == pytest.approx(1.0)


# --- ranking ----------------------------------------------------------------


def test_ranking_puts_safety_before_comfort_regardless_of_severity() -> None:
    results = [
        ScorerResult(name="x", flags=[_flag("stop_density", "s00000", Tier.COMFORT, severity=1.0)]),
        ScorerResult(name="y", flags=[_flag("legality", "s00009", Tier.SAFETY, severity=0.01)]),
    ]
    ranked = rank_flags(results, SEGMENTS, TOTAL_M)
    assert ranked[0].flag.tier is Tier.SAFETY


def test_ranking_puts_hard_before_soft_within_a_tier() -> None:
    results = [
        ScorerResult(
            name="x",
            flags=[
                _flag("segment_hostility", "s00000", Tier.SAFETY, FlagKind.SOFT, 0.9),
                _flag("segment_hostility", "s00001", Tier.SAFETY, FlagKind.HARD, 0.1),
            ],
        )
    ]
    ranked = rank_flags(results, SEGMENTS, TOTAL_M)
    assert ranked[0].flag.kind is FlagKind.HARD


def test_a_late_segment_outranks_an_identical_early_one() -> None:
    """The whole point of scope 8.2."""
    results = [
        ScorerResult(
            name="x",
            flags=[
                _flag("segment_hostility", SEGMENTS[0].id, Tier.COMFORT, severity=0.5),
                _flag("segment_hostility", SEGMENTS[-1].id, Tier.COMFORT, severity=0.5),
            ],
        )
    ]
    ranked = rank_flags(results, SEGMENTS, TOTAL_M)
    assert ranked[0].flag.segment_id == SEGMENTS[-1].id
    assert ranked[0].weighted > ranked[1].weighted


def test_locked_segments_are_excluded_from_ranking() -> None:
    """Scope 8.1 step 6: no point proposing a fix the loop may not apply."""
    results = [
        ScorerResult(
            name="x",
            flags=[
                _flag("segment_hostility", SEGMENTS[0].id, Tier.SAFETY),
                _flag("segment_hostility", SEGMENTS[1].id, Tier.SAFETY),
            ],
        )
    ]
    ranked = rank_flags(results, SEGMENTS, TOTAL_M, exclude=frozenset({SEGMENTS[0].id}))
    assert [r.flag.segment_id for r in ranked] == [SEGMENTS[1].id]


def test_ranking_is_stable_across_input_order() -> None:
    """A golden expected.json must not reorder between runs."""
    flags = [
        _flag("segment_hostility", SEGMENTS[i].id, Tier.SAFETY, severity=0.5) for i in range(4)
    ]
    forward = rank_flags([ScorerResult(name="x", flags=flags)], SEGMENTS, TOTAL_M)
    backward = rank_flags([ScorerResult(name="x", flags=list(reversed(flags)))], SEGMENTS, TOTAL_M)
    assert [r.flag.segment_id for r in forward] == [r.flag.segment_id for r in backward]


def test_flag_on_an_unknown_segment_does_not_crash_ranking() -> None:
    results = [ScorerResult(name="x", flags=[_flag("legality", "ghost", Tier.SAFETY)])]
    assert len(rank_flags(results, SEGMENTS, TOTAL_M)) == 1


def test_residual_flags_are_always_listed() -> None:
    """Scope 8.4: a route that could not be improved is not a route without problems."""
    results = [ScorerResult(name="x", flags=[_flag("surface_profile", "s00000", Tier.COMFORT)])]
    assert len(residual_flags(results, SEGMENTS, TOTAL_M)) == 1


# --- comparing alternatives -------------------------------------------------


def _candidate(label: str, flags: list[Flag]) -> object:
    return score_candidate(label, [ScorerResult(name="x", flags=flags)], SEGMENTS, TOTAL_M)


def test_a_hard_safety_flag_loses_to_anything_however_shady() -> None:
    """Scope 8.4: the ordering makes this trade unrepresentable, not merely discouraged."""
    unsafe = _candidate(
        "A", [_flag("segment_hostility", "s00000", Tier.SAFETY, FlagKind.HARD, 0.1)]
    )
    ugly = _candidate(
        "B",
        [_flag("surface_profile", f"s0000{i}", Tier.COMFORT, FlagKind.SOFT, 1.0) for i in range(5)],
    )
    assert compare(unsafe, ugly, segment_id="s00000").winner == "B"


def test_fewer_hard_flags_wins_outright() -> None:
    two = _candidate(
        "A",
        [
            _flag("legality", "s00000", Tier.SAFETY, FlagKind.HARD),
            _flag("legality", "s00001", Tier.SAFETY, FlagKind.HARD),
        ],
    )
    one = _candidate("B", [_flag("legality", "s00000", Tier.SAFETY, FlagKind.HARD)])
    assert compare(two, one, segment_id="s00000").winner == "B"


def test_a_hard_flag_difference_is_never_a_trade_off() -> None:
    """Even a one-flag gap is categorical, not a matter of taste."""
    unsafe = _candidate("A", [_flag("legality", "s00000", Tier.SAFETY, FlagKind.HARD)])
    safe = _candidate("B", [])
    result = compare(unsafe, safe, segment_id="s00000")
    assert result.winner == "B"
    assert result.trade_off is None


def test_a_clear_within_tier_win_is_decided() -> None:
    mild = _candidate("A", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.1)])
    severe = _candidate("B", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.9)])
    assert compare(mild, severe, segment_id="s00000").winner == "A"


def test_a_close_within_tier_call_is_handed_back_to_the_user() -> None:
    """Scope 8.4: same-tier conflicts are not resolved silently."""
    a = _candidate("A", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.50)])
    b = _candidate("B", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.55)])
    result = compare(a, b, segment_id="s00000")
    assert result.winner is None
    assert result.needs_input
    assert result.trade_off is not None
    assert result.trade_off.segment_id == "s00000"


def test_the_trade_off_carries_a_human_comparison() -> None:
    a = _candidate("A", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.50)])
    b = _candidate("B", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.55)])
    result = compare(
        a,
        b,
        segment_id="s00000",
        describe=lambda x, y: "A adds 0.8 km; B is shaded but lacks sidewalk at mile 41",
    )
    assert result.trade_off is not None
    assert "sidewalk" in result.trade_off.comparison


def test_identical_candidates_do_not_ask_a_pointless_question() -> None:
    flags = [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.5)]
    result = compare(_candidate("A", flags), _candidate("B", list(flags)), "s00000")
    assert result.winner == "A"
    assert result.trade_off is None


def test_margin_is_configurable() -> None:
    a = _candidate("A", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.50)])
    b = _candidate("B", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.55)])
    assert compare(a, b, "s00000", margin=0.0).winner == "A"
    assert compare(a, b, "s00000", margin=TRADEOFF_MARGIN).needs_input


def test_a_higher_tier_difference_beats_a_close_lower_tier_one() -> None:
    """Physiological separation settles it even if comfort is a coin flip."""
    thirsty = _candidate(
        "A", [_flag("resupply_schedule", "s00000", Tier.PHYSIOLOGICAL, severity=0.8)]
    )
    fine = _candidate("B", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.79)])
    assert compare(thirsty, fine, "s00000").winner == "B"


def test_candidate_worst_returns_the_top_flags() -> None:
    flags = [
        _flag("segment_hostility", SEGMENTS[i].id, Tier.COMFORT, severity=0.1 * i)
        for i in range(1, 6)
    ]
    assert len(_candidate("A", flags).worst(3)) == 3


def test_an_empty_route_scores_cleanly() -> None:
    empty = score_candidate("A", [], SEGMENTS, TOTAL_M)
    assert empty.tier_key() == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_a_clean_route_beats_a_flagged_one() -> None:
    clean = _candidate("A", [])
    flagged = _candidate("B", [_flag("surface_profile", "s00000", Tier.COMFORT, severity=0.9)])
    assert compare(clean, flagged, "s00000").winner == "A"
