"""Schema tests (scope 6.1, 6.3, 6.4, 7.10, 8.3, 8.4).

These pin the decisions that are cheap now and brutal to retrofit: segment identity,
flag ordering, provenance precedence, and the rule that sun is not assumed bad.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from longrun.core.models.context import Budget, BudgetExceeded, Clock, FrozenClock
from longrun.core.models.coverage import CoverageEntry, CoverageManifest
from longrun.core.models.features import Feature, FeatureSet
from longrun.core.models.geometry import BBox, Route, RoutePoint, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.models.plan import Manifest, Plan, ToolCall
from longrun.core.models.profile import (
    PreferenceEntry,
    PreferenceProfile,
    Provenance,
    SunPreference,
)
from longrun.core.models.request import LockedRange, PlanRequest, TimeWindow


def _route(n: int = 3, step: float = 500.0) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.001, lon=-122.4, cum_dist_m=i * step) for i in range(n)
        ],
    )


def _flag(seg: str, tier: Tier, kind: FlagKind, sev: float) -> Flag:
    return Flag(scorer="h", segment_id=seg, kind=kind, tier=tier, severity=sev, reason_code="rc")


# --- geometry ---------------------------------------------------------------


def test_route_requires_two_points() -> None:
    with pytest.raises(ValidationError, match="at least two points"):
        Route(id="r", points=[RoutePoint(lat=37.0, lon=-122.0)])


def test_route_rejects_backwards_distance() -> None:
    """cum_dist_m underpins pacing, position weighting and distance markers."""
    with pytest.raises(ValidationError, match="non-decreasing"):
        Route(
            id="r",
            points=[
                RoutePoint(lat=37.0, lon=-122.0, cum_dist_m=100.0),
                RoutePoint(lat=37.1, lon=-122.0, cum_dist_m=50.0),
            ],
        )


def test_route_length_is_final_cumulative_distance() -> None:
    assert _route(3, 500.0).length_m == 1000.0


def test_segment_span_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="end_idx must exceed"):
        Segment(id="s", index=0, start_idx=4, end_idx=4, cum_start_m=0, length_m=10)


def test_segment_end_distance_is_derived() -> None:
    s = Segment(id="s", index=0, start_idx=0, end_idx=2, cum_start_m=250, length_m=250)
    assert s.cum_end_m == 500.0


def test_bbox_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError, match="minimums must not exceed"):
        BBox(min_lon=-121.0, min_lat=37.0, max_lon=-122.0, max_lat=38.0)


def test_latitude_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        RoutePoint(lat=91.0, lon=0.0)


# --- measurement and arbitration ordering -----------------------------------


def test_tier_is_lexicographic_not_a_coefficient() -> None:
    """Scope 8.4: a shadier option never outranks a hard-safety one, whatever its weight."""
    assert Tier.SAFETY < Tier.PHYSIOLOGICAL < Tier.COMFORT


def test_severity_must_be_a_fraction() -> None:
    with pytest.raises(ValidationError):
        _flag("s", Tier.COMFORT, FlagKind.SOFT, 1.4)


def test_worst_orders_by_tier_then_hardness_then_severity() -> None:
    result = ScorerResult(
        name="h",
        flags=[
            _flag("s3", Tier.COMFORT, FlagKind.SOFT, 0.99),
            _flag("s1", Tier.SAFETY, FlagKind.SOFT, 0.10),
            _flag("s2", Tier.SAFETY, FlagKind.HARD, 0.10),
        ],
    )
    assert [f.segment_id for f in result.worst()] == ["s2", "s1", "s3"]


def test_worst_tie_break_is_total_so_goldens_do_not_flap() -> None:
    """Without a total order, expected.json reorders between runs and tests go flaky."""
    flags = [
        _flag("s_b", Tier.SAFETY, FlagKind.HARD, 0.5),
        _flag("s_a", Tier.SAFETY, FlagKind.HARD, 0.5),
    ]
    forward = ScorerResult(name="h", flags=flags).worst()
    reverse = ScorerResult(name="h", flags=list(reversed(flags))).worst()
    assert [f.segment_id for f in forward] == ["s_a", "s_b"]
    assert [f.segment_id for f in reverse] == ["s_a", "s_b"]


def test_worst_respects_the_requested_count() -> None:
    result = ScorerResult(
        name="h",
        flags=[_flag(f"s{i}", Tier.COMFORT, FlagKind.SOFT, 0.5) for i in range(9)],
    )
    assert len(result.worst(3)) == 3


def test_has_hard_flag() -> None:
    soft = ScorerResult(name="h", flags=[_flag("s", Tier.COMFORT, FlagKind.SOFT, 0.9)])
    hard = ScorerResult(name="h", flags=[_flag("s", Tier.SAFETY, FlagKind.HARD, 0.1)])
    assert not soft.has_hard_flag
    assert hard.has_hard_flag


def test_measurement_confidence_degrades_rather_than_dropping_the_value() -> None:
    """Scope 12: a missing tag is unknown, not absent."""
    m = SegmentMeasurement(segment_id="s", values={"lts": 3}, confidence=0.4)
    assert m.values["lts"] == 3
    assert m.confidence == pytest.approx(0.4)


# --- coverage ---------------------------------------------------------------


def test_manifest_separates_checked_from_unchecked() -> None:
    m = CoverageManifest()
    m.record(CoverageEntry(source="OSM", kind="hostility", checked=True))
    m.record(
        CoverageEntry(
            source="WZDx",
            kind="closures",
            checked=False,
            jurisdiction="0600000US",
            reason="no adapter registered",
        )
    )
    assert [e.source for e in m.checked()] == ["OSM"]
    assert [e.source for e in m.unchecked()] == ["WZDx"]
    assert "NOT CHECKED" in m.render()


def test_render_names_the_unchecked_source_and_why() -> None:
    m = CoverageManifest()
    m.record(CoverageEntry(source="511", kind="closures", checked=False, reason="no API key"))
    rendered = m.render()
    assert "511" in rendered
    assert "no API key" in rendered


# --- preference profile -----------------------------------------------------


def test_sun_is_not_assumed_bad() -> None:
    """Scope 6.3: at weight 0 the shade fraction is reported, never costed."""
    assert PreferenceProfile().scores_sun is False


def test_sun_scored_once_a_weight_is_set() -> None:
    hot = PreferenceProfile(sun=PreferenceEntry(value=SunPreference(hot=0.3)))
    assert hot.scores_sun is True


def test_defaults_match_the_scope_table() -> None:
    p = PreferenceProfile()
    assert p.water_gap_max_min.value == 90.0
    assert p.toilet_gap_max_min.value == 150.0
    assert p.carry_capacity_ml.value == 500.0
    assert p.traffic_tolerance.value == 2
    assert p.detour_tolerance_pct.value == 10.0
    assert p.stops_tolerance.value == 3.0
    assert p.darkness_tolerance.value is False
    assert p.scenery_vs_directness.value == 0.0
    assert p.sun.value.pivot_c == 22.0
    assert p.grade.value.max_climb_pct == 10.0
    assert p.grade.value.max_descent_pct == 10.0


def test_every_entry_defaults_to_default_provenance() -> None:
    assert PreferenceProfile().traffic_tolerance.provenance is Provenance.DEFAULT


def test_stated_supersedes_inferred_but_not_the_reverse() -> None:
    """Scope 6.3: a guess must never silently overwrite something the user asserted."""
    stated = PreferenceEntry[int](value=1, provenance=Provenance.STATED)
    inferred = PreferenceEntry[int](value=2, provenance=Provenance.INFERRED)
    default = PreferenceEntry[int](value=3, provenance=Provenance.DEFAULT)
    assert stated.supersedes(inferred)
    assert inferred.supersedes(default)
    assert not inferred.supersedes(stated)
    assert not default.supersedes(stated)


def test_weight_is_bounded() -> None:
    with pytest.raises(ValidationError):
        PreferenceEntry[int](value=1, weight=2.0)


# --- adapter features -------------------------------------------------------


def test_tier_four_extraction_cannot_claim_high_confidence() -> None:
    """Scope 7.10 caps LLM extraction at 0.5 and marks it unverified."""
    with pytest.raises(ValidationError, match="tier-4"):
        Feature(kind="closures", category="lane", geometry={}, tier=4, confidence=0.9)


def test_structured_feed_may_be_confident() -> None:
    f = Feature(kind="closures", category="lane", geometry={}, tier=1, confidence=0.95)
    assert f.confidence == pytest.approx(0.95)


def test_feature_rejects_end_before_start() -> None:
    with pytest.raises(ValidationError, match="end precedes"):
        Feature(
            kind="closures",
            category="lane",
            geometry={},
            tier=1,
            confidence=0.9,
            start=datetime(2026, 3, 15, 12),
            end=datetime(2026, 3, 15, 9),
        )


def test_active_at_treats_open_bounds_as_unbounded() -> None:
    f = Feature(
        kind="closures",
        category="lane",
        geometry={},
        tier=1,
        confidence=0.9,
        start=datetime(2026, 3, 15, 8),
    )
    assert f.active_at(datetime(2026, 3, 15, 9))
    assert not f.active_at(datetime(2026, 3, 15, 7))


def test_nothing_found_differs_from_nobody_asked() -> None:
    """An empty result with no jurisdictions queried is not evidence of no closures."""
    asked = FeatureSet(queried=["0600000US"])
    unasked = FeatureSet(missing_adapters=["0600000US"])
    assert asked.features == []
    assert asked.queried == ["0600000US"]
    assert unasked.queried == []
    assert unasked.missing_adapters == ["0600000US"]


# --- request ----------------------------------------------------------------


def test_generate_mode_requires_endpoints() -> None:
    with pytest.raises(ValidationError, match="needs a start and an end"):
        PlanRequest(mode="generate", date=date(2026, 3, 15))


def test_repair_mode_needs_no_endpoints() -> None:
    """Scope 6.1: in repair mode the user supplies the geometry."""
    assert PlanRequest(mode="repair", date=date(2026, 3, 15)).start is None


def test_start_time_and_window_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        PlanRequest(
            mode="repair",
            date=date(2026, 3, 15),
            start_time=time(7, 30),
            start_window=TimeWindow(earliest=time(6), latest=time(9)),
        )


def test_time_window_must_not_invert() -> None:
    with pytest.raises(ValidationError, match="ends before"):
        TimeWindow(earliest=time(9), latest=time(6))


def test_locked_range_must_have_length() -> None:
    with pytest.raises(ValidationError, match="positive length"):
        LockedRange(start_m=100.0, end_m=100.0)


# --- context and budget -----------------------------------------------------


def test_frozen_clock_satisfies_the_protocol() -> None:
    assert isinstance(FrozenClock(datetime(2026, 3, 15, 7, 30)), Clock)


def test_api_budget_is_enforced() -> None:
    b = Budget(api_calls_max=2)
    b.spend_api_call(2)
    with pytest.raises(BudgetExceeded, match="API budget"):
        b.spend_api_call()


def test_imagery_cap_is_enforced() -> None:
    """Scope 7.9 caps imagery at ~10 tiles per plan."""
    b = Budget(imagery_tiles_max=1)
    b.spend_imagery_tile()
    with pytest.raises(BudgetExceeded, match="imagery budget"):
        b.spend_imagery_tile()


def test_deadline_is_checked_against_the_injected_clock() -> None:
    b = Budget(deadline=datetime(2026, 3, 15, 7, 0))
    b.check_deadline(FrozenClock(datetime(2026, 3, 15, 6, 59)))
    with pytest.raises(BudgetExceeded, match="deadline"):
        b.check_deadline(FrozenClock(datetime(2026, 3, 15, 7, 1)))


# --- plan -------------------------------------------------------------------


def test_manifest_totals_elapsed_time() -> None:
    m = Manifest()
    m.record(ToolCall(tool="route", elapsed_s=1.5))
    m.record(ToolCall(tool="sun_exposure", elapsed_s=2.25, cached=True))
    assert m.total_elapsed_s == pytest.approx(3.75)


def test_plan_round_trips_through_json() -> None:
    """The plan schema is the API contract (scope 10.3), so it must serialize whole."""
    plan = Plan(
        id="p1",
        request=PlanRequest(
            mode="repair",
            date=date(2026, 3, 15),
            time_constraints={"total_budget": timedelta(hours=6)},
        ),
        route=_route(),
        segments=[Segment(id="s0", index=0, start_idx=0, end_idx=1, cum_start_m=0, length_m=500)],
        results=[ScorerResult(name="legality")],
        etas=[datetime(2026, 3, 15, 7, 30), datetime(2026, 3, 15, 8, 0)],
    )
    restored = Plan.model_validate_json(plan.model_dump_json())
    assert restored.id == "p1"
    assert restored.route.length_m == plan.route.length_m
    assert restored.finish_time == datetime(2026, 3, 15, 8, 0)
    assert restored.request.time_constraints.total_budget == timedelta(hours=6)


def test_result_lookup_by_scorer_name() -> None:
    plan = Plan(
        id="p",
        request=PlanRequest(mode="repair", date=date(2026, 3, 15)),
        route=_route(),
    )
    assert plan.result("legality") is None
