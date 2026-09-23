"""A carried measurement finds its segment by distance, not by index (M11.3, ADR 0032).

`segment_id(index)` is `f"s{index:05d}"`, so an edit that inserts or removes a single point
renumbers every segment after it. The dangerous part is that the old id stays *valid*:
`s00042` names a segment either way, a few hundred metres from the one it was measured on.
Nothing in the tree noticed, because `rescore_plan` never re-routed and `run_scorers` merged
carried results by scorer name alone.

These tests are about the two ways that goes wrong and the one way it does not:

* the ids shift, and a carried measurement follows its **ground** or is dropped;
* the ids *do not* shift - an equal-length replacement in the middle leaves every distance
  downstream untouched - and a span match alone would silently re-point the replaced
  stretch, so the ground is checked as well;
* the line did not move at all, which is every refresh, and then nothing changes.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from longrun.core.geo.gpx import haversine_m
from longrun.core.geo.segments import map_segments, segment_route
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.models.waypoint import PlanWaypoint
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.registry import UngroundedCarry, carry, run_scorers

MEASURED_AT = datetime(2026, 3, 15, 7, 30)

#: A metre of longitude at this latitude, near enough: the fixtures are 100 m apart.
STEP_DEG = 0.00114
BASE_LAT = 37.77
BASE_LON = -122.42


def _measured(coords: list[tuple[float, float]]) -> Route:
    """A route whose `cum_dist_m` is accumulated from its own geometry, as a real one is."""
    points: list[RoutePoint] = []
    total = 0.0
    for index, (lat, lon) in enumerate(coords):
        if index:
            previous = coords[index - 1]
            total += haversine_m(previous[0], previous[1], lat, lon)
        points.append(RoutePoint(lat=lat, lon=lon, cum_dist_m=total))
    return Route(id="r", points=points)


def _pinned(lats: list[float]) -> Route:
    """A route with its distances **pinned** to 100 m a step, whatever the geometry says.

    Deliberately unphysical, and that is the point: it is how a fixture reproduces the case
    a span match alone cannot survive - two lines that agree about every distance along the
    route and disagree about where the route is.
    """
    return Route(
        id="r",
        points=[
            RoutePoint(lat=lat, lon=BASE_LON + index * STEP_DEG, cum_dist_m=index * 100.0)
            for index, lat in enumerate(lats)
        ],
    )


def _east(count: int = 20) -> list[tuple[float, float]]:
    return [(BASE_LAT, BASE_LON + index * STEP_DEG) for index in range(count)]


def _stored(ids: list[str]) -> ScorerResult:
    return ScorerResult(
        name="segment_hostility",
        measurements=[
            SegmentMeasurement(segment_id=name, values={"lts": float(index)})
            for index, name in enumerate(ids)
        ],
    )


# --- the line did not move ----------------------------------------------------


def test_an_unchanged_line_maps_every_segment_onto_itself() -> None:
    """Every refresh. ADR 0030: a refresh passes no router, so the line cannot have moved."""
    route = _measured(_east())
    segments = segment_route(route)

    onto = map_segments(route, segments, route, segments)

    assert onto.moved is False
    assert onto.onto == {segment.id: segment.id for segment in segments}
    assert onto.lost == ()


def test_a_different_elevation_reading_is_not_a_different_place() -> None:
    """`_score_pass` writes terrain heights onto the points it returns, so two readings of
    one line differ in `ele_m` whenever one ran on a machine with a DEM and the other did
    not. That is a difference in what was measured, not in where the segment is."""
    route = _measured(_east())
    raised = route.model_copy(
        update={"points": [p.model_copy(update={"ele_m": 12.0}) for p in route.points]}
    )
    segments = segment_route(route)

    assert map_segments(route, segments, raised, segment_route(raised)).moved is False


def test_carrying_across_an_unchanged_line_changes_nothing_at_all() -> None:
    """The bit-exactness `longrun refresh` depends on, stated as behaviour."""
    route = _measured(_east())
    segments = segment_route(route)
    stored = _stored([segment.id for segment in segments])

    carried = carry(stored, MEASURED_AT, map_segments(route, segments, route, segments))

    assert [m.segment_id for m in carried.measurements] == [s.id for s in segments]
    assert carried.regrounded is None, "no move happened, so there is nothing to report"


# --- the ids shifted ----------------------------------------------------------


def _detoured() -> Route:
    """The same line with five points in the middle replaced by a six-point northward jog.

    One point more and a longer middle, so every segment after it is both renumbered *and*
    at a different distance - the shape of every real geometry edit.
    """
    coords = _east()
    bulge = [(BASE_LAT + 0.0015, BASE_LON + (10 + step) * STEP_DEG) for step in range(6)]
    return _measured(coords[:10] + bulge + coords[15:])


def test_a_segment_before_the_edit_keeps_its_ground_and_a_segment_after_it_is_lost() -> None:
    """Both halves in one assertion, because either alone permits the bug: a map that kept
    nothing is useless, and one that kept everything is the bug."""
    old_route = _measured(_east())
    new_route = _detoured()
    old, new = segment_route(old_route), segment_route(new_route)

    onto = map_segments(old_route, old, new_route, new)

    assert onto.moved is True
    assert onto.onto, "the stretch before the edit is untouched and must survive"
    assert onto.lost, "the stretch after it moved down the route and must not"
    # Everything kept is at the same distance in both segmentations, which is the claim.
    by_id = {segment.id: segment for segment in new}
    for old_segment in old:
        landed = onto.onto.get(old_segment.id)
        if landed is None:
            continue
        assert abs(by_id[landed].cum_start_m - old_segment.cum_start_m) < 1.0


def test_a_carried_measurement_is_dropped_rather_than_left_pointing_at_new_ground() -> None:
    """The failure this milestone exists for.

    Before M11.3 a carried `ScorerResult` kept its `segment_id`s verbatim, so a measurement
    taken at 1.4 km on the old line was presented as a measurement of whatever `s00007`
    happens to be on the new one.
    """
    old_route = _measured(_east())
    new_route = _detoured()
    old, new = segment_route(old_route), segment_route(new_route)
    stored = _stored([segment.id for segment in old])

    carried = carry(stored, MEASURED_AT, map_segments(old_route, old, new_route, new))

    kept = {m.segment_id for m in carried.measurements}
    assert kept, "the untouched head of the route still has its measurements"
    assert len(kept) < len(old), "and the moved tail does not"
    assert carried.regrounded is not None
    assert carried.regrounded.measurements_dropped == len(old) - len(kept)
    # And nothing landed on a segment the new line does not have.
    assert kept <= {segment.id for segment in new}


def test_a_flag_follows_its_measurement_and_is_dropped_with_it() -> None:
    """A flag names a segment too, and a flag on the wrong segment is the same lie in the
    tier that decides whether a route is safe."""
    old_route = _measured(_east())
    new_route = _detoured()
    old, new = segment_route(old_route), segment_route(new_route)
    gone = old[-1].id
    stored = ScorerResult(
        name="segment_hostility",
        flags=[
            Flag(
                scorer="segment_hostility",
                segment_id=name,
                kind=FlagKind.SOFT,
                tier=Tier.SAFETY,
                severity=0.5,
                reason_code="lts_above_tolerance",
            )
            for name in (old[0].id, gone)
        ],
    )

    carried = carry(stored, MEASURED_AT, map_segments(old_route, old, new_route, new))

    assert [f.segment_id for f in carried.flags] == [old[0].id]
    assert carried.regrounded is not None
    assert carried.regrounded.flags_dropped == 1


def test_a_route_total_is_dropped_because_the_route_is_what_changed() -> None:
    """`ROUTE_SUMMARY_ID`, a `start@07:00` sweep row and a `meet#0` crew point all describe
    the whole line. Nothing is wrong with the ground they cover - the number is simply
    about a different route - so they are counted apart from a lost segment."""
    old_route = _measured(_east())
    new_route = _detoured()
    old, new = segment_route(old_route), segment_route(new_route)
    stored = ScorerResult(
        name="segment_hostility",
        measurements=[
            SegmentMeasurement(segment_id=old[0].id, values={"lts": 2.0}),
            SegmentMeasurement(segment_id=ROUTE_SUMMARY_ID, values={"fraction_lts3_plus": 0.4}),
        ],
    )

    carried = carry(stored, MEASURED_AT, map_segments(old_route, old, new_route, new))

    assert [m.segment_id for m in carried.measurements] == [old[0].id]
    assert carried.regrounded is not None
    assert carried.regrounded.route_totals_dropped == 1
    assert carried.regrounded.measurements_dropped == 0, "a route total is not a lost segment"


def test_a_waypoint_past_the_edit_does_not_keep_its_kilometre_mark() -> None:
    """A `PlanWaypoint` carries no segment id - it is keyed on `cum_dist_m`, a distance along
    the line that was just edited. A fountain at 1.6 km on the old route is at some other
    distance on the new one, and a course point placed from the old number is in the wrong
    place."""
    old_route = _measured(_east())
    new_route = _detoured()
    old, new = segment_route(old_route), segment_route(new_route)
    stored = ScorerResult(
        name="services_along",
        waypoints=[
            PlanWaypoint(
                position={"lat": BASE_LAT, "lon": BASE_LON},
                kind="water",
                label="near the start",
                cum_dist_m=50.0,
                scorer="services_along",
            ),
            PlanWaypoint(
                position={"lat": BASE_LAT, "lon": BASE_LON + 18 * STEP_DEG},
                kind="water",
                label="past the detour",
                cum_dist_m=old_route.length_m - 50.0,
                scorer="services_along",
            ),
        ],
    )

    carried = carry(stored, MEASURED_AT, map_segments(old_route, old, new_route, new))

    assert [w.label for w in carried.waypoints] == ["near the start"]
    assert carried.regrounded is not None
    assert carried.regrounded.waypoints_dropped == 1


# --- the ids did *not* shift, and the ground did ------------------------------


def test_the_same_distances_over_different_ground_carry_nothing() -> None:
    """Why a span match is not enough on its own.

    Two routes that agree about every distance along the route and disagree about where the
    route is produce *identical* segmentations - same ids, same spans, same point indices -
    so matching on `cum_start_m` and `length_m` alone would carry every measurement onto a
    line three hundred metres away and report nothing.
    """
    old_route = _pinned([BASE_LAT] * 20)
    new_route = _pinned([BASE_LAT + 0.003] * 20)
    old, new = segment_route(old_route), segment_route(new_route)

    assert [s.model_dump() for s in old] == [s.model_dump() for s in new], (
        "the fixture is only interesting if the two segmentations are identical"
    )

    onto = map_segments(old_route, old, new_route, new)

    assert onto.moved is True
    assert onto.onto == {}
    assert set(onto.lost) == {segment.id for segment in old}


def test_an_equal_length_replacement_in_the_middle_loses_only_the_middle() -> None:
    """The counter-example that is not exotic: swap a stretch for one of the same length and
    every distance downstream is unchanged, so the tail is genuinely still there."""
    lats = [BASE_LAT] * 20
    moved = _pinned([lat + 0.002 if 8 <= index <= 12 else lat for index, lat in enumerate(lats)])
    old_route = _pinned(lats)
    old, new = segment_route(old_route), segment_route(moved)

    onto = map_segments(old_route, old, moved, new)

    assert onto.onto, "the head and the tail did not move"
    assert onto.lost, "the replaced middle did"
    displaced = {s.id for s in old if s.start_idx in range(8, 13) or s.end_idx in range(8, 13)}
    assert set(onto.lost) == displaced


# --- and the refusal ----------------------------------------------------------


def test_carrying_with_no_segmentation_to_carry_from_is_refused() -> None:
    """Refused rather than assumed, for the reason `StalePrior` is: assuming produces a
    wrong number rather than a missing one, and a wrong one is silent."""
    route = _measured(_east())
    with pytest.raises(UngroundedCarry, match="positional"):
        run_scorers(
            route,
            segment_route(route),
            _ctx(),
            [MEASURED_AT] * len(route.points),
            only={"lighting"},
            carried=[_stored(["s00000"])],
            carried_as_of=MEASURED_AT,
        )


def _ctx() -> object:
    from longrun.core.data.cache import SqliteCache
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore
    from longrun.core.models.context import Budget, FrozenClock, ScorerContext
    from longrun.core.models.coverage import CoverageManifest
    from longrun.core.preferences.store import load_defaults

    return ScorerContext(
        layers=FileLayerStore("."),
        rasters=FileRasterStore("."),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(MEASURED_AT),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )
