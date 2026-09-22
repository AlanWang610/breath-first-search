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
