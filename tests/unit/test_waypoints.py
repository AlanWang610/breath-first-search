"""`PlanWaypoint`, and the merge that puts every scorer's finds on one plan (scope 9).

The model half. What each scorer emits is tested beside that scorer; this is about the
shape, the dedup and the ordering - and about the property that makes the whole design
affordable, which is that a waypoint is invisible to the golden content hash.
"""

from __future__ import annotations

from datetime import datetime

from longrun.core.models.geometry import LatLon
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.models.waypoint import PlanWaypoint
from longrun.core.plan.pipeline import merge_waypoints

AT = datetime(2026, 3, 15, 8, 14)


def _waypoint(
    kind: str = "water",
    *,
    lat: float = 37.7749,
    lon: float = -122.4194,
    cum: float = 100.0,
    scorer: str = "services_along",
    label: str = "fountain",
) -> PlanWaypoint:
    return PlanWaypoint(
        position=LatLon(lat=lat, lon=lon),
        kind=kind,  # type: ignore[arg-type]
        label=label,
        cum_dist_m=cum,
        scorer=scorer,
    )


def test_a_waypoint_carries_a_real_time_which_a_measurement_could_not() -> None:
    """`crew_points` stores its ETA as an "%H:%M" string because `values` holds scalars.

    A sibling list has no such constraint, and both course formats need a timestamp - so
    this is the concrete payoff of not putting waypoints in `SegmentMeasurement.values`.
    """
    waypoint = _waypoint().model_copy(update={"eta": AT})
    assert waypoint.eta == AT
    assert isinstance(waypoint.position, LatLon)


def test_a_waypoint_is_invisible_to_the_golden_content_hash() -> None:
    """The property the whole design rests on, asserted rather than assumed.

    `measurements_hash` walks `result.measurements` and `measurement.values`. A waypoint is
    on neither, so recording positions moves no `measurements_sha256` on any route - which
    is what keeps M10's golden diff to a reviewed count block instead of six rewritten files.
    """
    from tests.golden.expectation import measurements_hash

    class _Plan:
        results = [
            ScorerResult(
                name="services_along",
                measurements=[SegmentMeasurement(segment_id="s0", values={"a": 1.0})],
            )
        ]

    bare = measurements_hash(_Plan())  # type: ignore[arg-type]
    _Plan.results[0].waypoints.append(_waypoint())
    assert measurements_hash(_Plan()) == bare  # type: ignore[arg-type]


def test_the_merge_sorts_along_the_route() -> None:
    results = [
        ScorerResult(
            name="services_along",
            waypoints=[
                _waypoint(cum=900.0, lon=-122.40, label="late"),
                _waypoint(cum=100.0, lon=-122.41, label="early"),
            ],
        )
    ]
    assert [w.label for w in merge_waypoints(results)] == ["early", "late"]


def test_the_same_fountain_found_by_two_scorers_appears_once() -> None:
    """`services_along` and `resupply_schedule` read the same nodes layer with the same
    buffer, so every fountain arrives twice on every plan that runs both."""
    shared = {"lat": 37.7749, "lon": -122.4194}
    results = [
        ScorerResult(
            name="services_along",
            waypoints=[_waypoint(scorer="services_along", **shared)],
        ),
        ScorerResult(
            name="resupply_schedule",
            waypoints=[_waypoint(scorer="resupply_schedule", label="fountain (open)", **shared)],
        ),
    ]
    merged = merge_waypoints(results)
    assert len(merged) == 1
    # The resupply copy wins: it knows whether the place is open at the runner's arrival,
    # which is strictly more about the same point.
    assert merged[0].scorer == "resupply_schedule"


def test_two_kinds_at_one_position_are_two_waypoints() -> None:
    """A tap outside a public toilet is both, and dropping either would be wrong."""
    shared = {"lat": 37.7749, "lon": -122.4194}
    results = [
        ScorerResult(
            name="services_along",
            waypoints=[_waypoint(kind="water", **shared), _waypoint(kind="toilet", **shared)],
        )
    ]
    assert len(merge_waypoints(results)) == 2


def test_a_scorer_that_found_nothing_contributes_nothing() -> None:
    """An `unavailable` scorer must not fabricate a waypoint - `boston-winter` returns
    `unavailable` from three of the six that emit them."""
    assert merge_waypoints([ScorerResult(name="services_along")]) == []
