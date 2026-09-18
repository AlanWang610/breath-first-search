"""`route_diff` and `result_diff` (scope 7.8).

`diff.py` has had no unit test since it was written in M5.7 - it was exercised only through
the `route_diff` MCP tool and one call inside `check_10_locks_intact`. M10 adds `result_diff`
and gives both halves the tests the module never had.
"""

from __future__ import annotations

from datetime import datetime

from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, Tier
from longrun.core.plan.diff import SAME_LINE_M, result_diff, route_diff

MEASURED_AT = datetime(2026, 3, 15, 7, 30)


def _route(lat: float = 37.7749, points: int = 11) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=lat, lon=-122.4194 + i * 0.00114, cum_dist_m=i * 100.0)
            for i in range(points)
        ],
    )


def _flag(code: str, kind: FlagKind = FlagKind.SOFT, segment: str = "s0") -> Flag:
    return Flag(
        scorer="closures",
        segment_id=segment,
        kind=kind,
        tier=Tier.SAFETY,
        severity=1.0,
        reason_code=code,
    )


# --- route_diff, which had no test at all -----------------------------------------------


def test_the_same_line_compared_with_itself_reports_no_difference() -> None:
    diff = route_diff(_route(), _route())
    assert diff.spans == []
    assert diff.length_delta_m == 0.0
    assert diff.shared_fraction == 1.0
    assert diff.summary() == "same line; +0 m"


def test_a_route_that_moves_sideways_reports_a_span() -> None:
    """One degree of latitude is ~111 km, so this is ~111 m north - well over SAME_LINE_M."""
    diff = route_diff(_route(), _route(lat=37.7749 + 0.001))
    assert diff.spans, f"a {0.001 * 111_000:.0f} m offset is more than {SAME_LINE_M} m"
    assert diff.shared_fraction == 0.0


# --- result_diff ------------------------------------------------------------------------


def test_a_closure_that_appeared_since_the_plan_was_made_is_named() -> None:
    """The reason a refresh reports anything at all: ADR 0013 lets closures hard-fail, so a
    new one is the single most important thing a refresh can tell you."""
    before = [ScorerResult(name="closures")]
    after = [ScorerResult(name="closures", flags=[_flag("road_closed", FlagKind.HARD)])]

    delta = result_diff(before, after).scorers[0]
    assert delta.appeared == ("road_closed",)
    assert delta.resolved == ()
    assert (delta.hard_before, delta.hard_after) == (0, 1)
    assert delta.moved
    assert "road_closed" in delta.line()


def test_a_closure_that_has_gone_is_named_too() -> None:
    before = [ScorerResult(name="closures", flags=[_flag("road_closed", FlagKind.HARD)])]
    after = [ScorerResult(name="closures")]

    delta = result_diff(before, after).scorers[0]
    assert delta.resolved == ("road_closed",)
    assert (delta.hard_before, delta.hard_after) == (1, 0)


def test_the_same_flag_on_a_different_segment_is_not_a_change() -> None:
    """Keyed on reason_code alone. Segment ids shift if the ways layer was reloaded, and a
    report that dissolved into every renumbering would be unreadable exactly when it matters.
    The counts still move if the number of flags moves."""
    before = [ScorerResult(name="closures", flags=[_flag("road_closed", segment="s0")])]
    after = [ScorerResult(name="closures", flags=[_flag("road_closed", segment="s7")])]

    delta = result_diff(before, after).scorers[0]
    assert delta.appeared == ()
    assert delta.resolved == ()
    assert not delta.moved


def test_a_carried_scorer_says_so_rather_than_saying_nothing_changed() -> None:
    """ "No change" and "not re-checked" are different claims and must not render alike."""
    before = [ScorerResult(name="legality")]
    after = [ScorerResult(name="legality", carried_from=MEASURED_AT)]

    delta = result_diff(before, after).scorers[0]
    assert delta.carried
    assert not delta.moved


def test_a_scorer_that_stopped_reporting_is_not_dropped_from_the_diff() -> None:
    """A scorer present before and absent after is itself a change worth seeing."""
    before = [ScorerResult(name="closures", flags=[_flag("road_closed")])]
    after: list[ScorerResult] = []

    names = [d.name for d in result_diff(before, after).scorers]
    assert names == ["closures"]
    assert result_diff(before, after).scorers[0].resolved == ("road_closed",)


def test_an_unchanged_scoring_says_so_once_rather_than_twenty_one_times() -> None:
    results = [ScorerResult(name=name) for name in ("legality", "closures", "lighting")]
    diff = result_diff(results, results)
    assert diff.moved == ()
    assert diff.summary() == "no scorer changed its flags"
