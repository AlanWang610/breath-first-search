"""A line that was changed says so (M11.5), and the two gestures that change one.

`Route.source` has carried the literal `"edited"` since M1 and nothing in `src/` ever
wrote it - nor `"generated"`, so every line a router drew claimed to have been imported.
A field with no producer is one every reader is free to be wrong about, and `route_diff`,
`gpx_verify` check 10 and the plan sheet all have a reason to ask.
"""

from __future__ import annotations

import io

import pytest

from longrun.core.geo.gpx import gpx_read, gpx_string
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.plan.edits import NotAnEdit, splice
from longrun.core.routing.graphhopper import path_to_route

BASE_LAT = 37.77
BASE_LON = -122.42
STEP_DEG = 0.00114


def _line(lat: float = BASE_LAT, count: int = 20, start: int = 0) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(
                lat=lat,
                lon=BASE_LON + (start + index) * STEP_DEG,
                ele_m=10.0,
                cum_dist_m=index * 100.0,
            )
            for index in range(count)
        ],
    )


def _path(lat: float, lons: list[float]) -> dict[str, object]:
    return {"points": {"coordinates": [[lon, lat] for lon in lons]}}


# --- who drew it --------------------------------------------------------------


def test_a_line_the_router_drew_is_not_called_imported() -> None:
    """`path_to_route` used to let `Route.source` default, so every generated plan reported
    a line the runner had supplied. Nothing read it, which is why it went unnoticed."""
    route = path_to_route(
        _path(BASE_LAT, [BASE_LON + index * STEP_DEG for index in range(5)]),
        route_id="generated",
    )
    assert route.source == "generated"


def test_a_matched_line_keeps_the_provenance_of_the_line_it_matched() -> None:
    """A match is a *reading* of the line it was given, snapped to the graph. Calling a
    matched user GPX "generated" would file the runner's own route under the router's name."""
    route = path_to_route(
        _path(BASE_LAT, [BASE_LON + index * STEP_DEG for index in range(5)]),
        route_id="track",
        source="imported",
    )
    assert route.source == "imported"


# --- and it survives the round trip -------------------------------------------


def test_provenance_survives_a_gpx_round_trip() -> None:
    """Scope 9 makes GPX an output format, so a plan whose line was edited, exported and
    read back would otherwise come back claiming the runner drew it."""
    edited = _line().model_copy(update={"source": "edited"})
    back = gpx_read(io.StringIO(gpx_string(edited)))
    assert back.source == "edited"


def test_a_gpx_that_says_nothing_about_provenance_is_the_runners_own() -> None:
    """Which is every GPX in the world that this project did not write - including a
    `<type>` of `road_biking`, which is a real GPX 1.1 value and not one of ours."""
    plain = gpx_string(_line()).replace("<type>imported</type>", "<type>road_biking</type>")
    assert gpx_read(io.StringIO(plain)).source == "imported"


# --- splicing an alternative in -----------------------------------------------


def test_a_spliced_line_is_marked_edited_and_keeps_its_identity() -> None:
    """The same plan with a different middle, not a different plan."""
    original = _line()
    detour = _line(lat=BASE_LAT + 0.002, count=6, start=8)

    out = splice(original, 800.0, 1300.0, detour)

    assert out.source == "edited"
    assert out.id == original.id


def test_a_splice_keeps_the_elevations_of_the_ground_it_did_not_touch() -> None:
    """Scope 7.1 says elevation comes from the terrain model, and the terrain under the
    untouched two-thirds of a route has not changed - so re-reading a DEM for it is work at
    best, and on a machine with no rasters it replaces a good profile with nulls.

    The replaced stretch reads as unknown until something samples it, which is the honest
    answer and which `ElevationProfile.samples_missing` already counts.
    """
    original = _line()
    detour = _line(lat=BASE_LAT + 0.002, count=6, start=8)

    out = splice(original, 800.0, 1300.0, detour)

    assert out.points[0].ele_m == 10.0, "the head keeps what it knew"
    assert out.points[-1].ele_m == 10.0, "and so does the tail"
    assert any(point.ele_m is None for point in out.points), "the new ground does not pretend"


def test_a_splice_that_would_consume_an_end_of_the_route_is_refused() -> None:
    """Not a splice at all: there would be no head to join to, and the result would be a
    different route wearing this one's id."""
    original = _line()
    with pytest.raises(NotAnEdit, match="consume one of its ends"):
        splice(original, 0.0, 1000.0, _line(lat=BASE_LAT + 0.002, count=4))


def test_a_zero_length_splice_is_an_error_rather_than_a_silent_no_op() -> None:
    original = _line()
    with pytest.raises(NotAnEdit, match="positive length"):
        splice(original, 500.0, 500.0, _line(lat=BASE_LAT + 0.002, count=4))


# --- redrawing through a request that has moved -------------------------------


class _StubRouter:
    """Records what it was asked for and answers with a line through the waypoints.

    A stub rather than a GraphHopper: `longrun edit reroute` is the one scope 10.3 gesture
    the hermetic suite cannot drive end to end, so what is testable is that the right
    question reaches the router.
    """

    name = "stub"

    def __init__(self) -> None:
        self.asked: dict[str, object] = {}

    def route(self, waypoints, profile="foot", avoid_polygons=None, custom_model=None):  # type: ignore[no-untyped-def]
        self.asked = {
            "waypoints": list(waypoints),
            "avoid_polygons": avoid_polygons,
            "custom_model": custom_model,
        }
        return Route(
            id="fresh",
            points=[
                RoutePoint(lat=p.lat, lon=p.lon, cum_dist_m=index * 100.0)
                for index, p in enumerate(waypoints)
            ],
            source="generated",
        )


def _pad(**policy: object):  # type: ignore[no-untyped-def]
    from datetime import date

    from longrun.core.models.geometry import LatLon
    from longrun.core.models.request import PlanRequest
    from longrun.core.models.routing import RoutingPolicy
    from longrun.core.plan.scratchpad import Scratchpad

    request = PlanRequest(
        date=date(2026, 3, 15),
        start=LatLon(lat=BASE_LAT, lon=BASE_LON),
        end=LatLon(lat=BASE_LAT, lon=BASE_LON + 10 * STEP_DEG),
    )
    return Scratchpad(
        plan_id="p",
        request=request,
        route=_line(),
        policy=RoutingPolicy(**policy),  # type: ignore[arg-type]
    )


def test_a_redrawn_line_is_marked_edited_and_keeps_the_plans_route_id() -> None:
    from longrun.core.plan.edits import redraw

    pad = _pad()
    line = redraw(pad, _StubRouter())  # type: ignore[arg-type]

    assert line.source == "edited"
    assert line.id == "r", "the same plan's line, redrawn - not a new route"


def test_a_redraw_goes_through_the_request_waypoints_in_order() -> None:
    """The same list `agent.loop` builds. A redraw that used a different one would produce a
    line the loop would not reproduce on its next round."""
    from longrun.core.models.geometry import LatLon
    from longrun.core.plan.edits import redraw

    pad = _pad()
    pad.add_via(LatLon(lat=BASE_LAT, lon=BASE_LON + 5 * STEP_DEG))
    router = _StubRouter()
    redraw(pad, router)  # type: ignore[arg-type]

    lons = [point.lon for point in router.asked["waypoints"]]  # type: ignore[union-attr]
    assert lons == sorted(lons)
    assert len(lons) == 3, "start, the new via, end"


def test_a_redraw_carries_the_stored_costing_model_rather_than_rebuilding_one() -> None:
    """What "frozen and resolved once" protects: two resolutions of one plan may differ, and
    the line this one replaces was drawn with the first."""
    from longrun.core.plan.edits import redraw

    pad = _pad(custom_model={"priority": [{"if": "true", "multiply_by": "0.5"}]})
    router = _StubRouter()
    redraw(pad, router)  # type: ignore[arg-type]

    assert router.asked["custom_model"] == pad.policy.custom_model


def test_a_redraw_honours_an_avoid_area_the_request_has_grown_since() -> None:
    """The half of the policy the runner has just changed. A redraw that ignored
    `request.avoid_polygons` would honour the gesture by doing nothing."""
    from longrun.core.plan.edits import redraw

    drawn = {"type": "Feature", "id": "avoid-0", "properties": {}, "geometry": {"type": "Polygon"}}
    pad = _pad()
    pad.request = pad.request.model_copy(update={"avoid_polygons": [drawn]})
    router = _StubRouter()
    redraw(pad, router)  # type: ignore[arg-type]

    assert router.asked["avoid_polygons"] == [drawn]


def test_an_area_already_in_the_policy_is_not_sent_twice() -> None:
    from longrun.core.plan.edits import redraw

    area = {"type": "Feature", "id": "avoid-0", "properties": {}, "geometry": {"type": "Polygon"}}
    pad = _pad(avoid_polygons=[area])
    pad.request = pad.request.model_copy(update={"avoid_polygons": [dict(area)]})
    router = _StubRouter()
    redraw(pad, router)  # type: ignore[arg-type]

    assert router.asked["avoid_polygons"] == [area]
