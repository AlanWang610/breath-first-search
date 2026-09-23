"""Editing a plan that already exists (scope 7.8, 10.3).

Scope 10.3's direct manipulation is five gestures - lock, unlock, choose an alternative,
add a via point, draw an avoid polygon - and every one of them is a write to a stored plan
rather than a new one. These tests are the CLI-shaped half of that: `ui/README.md` claims
"what is missing is the endpoints and the map interactions, not the capability", and this
file is where that claim is either true or gets fixed.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.models.request import LockedRange, PlanRequest, TimeWindow
from longrun.core.plan.scratchpad import Scratchpad


def _request() -> PlanRequest:
    return PlanRequest(
        date=date(2026, 3, 15),
        start=LatLon(lat=37.77, lon=-122.42),
        end=LatLon(lat=37.79, lon=-122.40),
    )


def _pad() -> Scratchpad:
    return Scratchpad(plan_id="p", request=_request())


def _straight_route(points: int = 12, step_m: float = 100.0) -> Route:
    """A line due east, spaced so `segment_route` makes more than one segment of it."""
    return Route(
        id="edit",
        points=[
            RoutePoint(lat=37.77, lon=-122.42 + index * 0.00114, cum_dist_m=index * step_m)
            for index in range(points)
        ],
    )


# --- M11.1: a lock says who wrote it -----------------------------------------


def test_a_lock_a_runner_set_and_one_the_loop_set_are_different_locks() -> None:
    """The discriminator "unlock what I locked" needs, and did not have.

    Both used to be a `LockedRange` with prose in `reason`, so the only way to tell them
    apart was to parse `"round 3: rerouted"` - a string the sheet is free to rephrase.
    """
    pad = _pad()
    pad.lock(0.0, 100.0, reason="I always run this bit")
    pad.lock(200.0, 300.0, reason="round 1: rerouted", source="loop")

    assert [lock.source for lock in pad.locked] == ["user", "loop"]


def test_a_lock_that_came_in_on_the_request_is_the_runners() -> None:
    """`PlanRequest.locked` is the request, so nobody but the runner put it there."""
    request = _request().model_copy(update={"locked": [LockedRange(start_m=0.0, end_m=50.0)]})
    assert request.locked[0].source == "user"


def test_a_lock_source_outside_the_two_that_exist_is_refused() -> None:
    """A third author would silently widen what `unlock(source=...)` leaves behind."""
    with pytest.raises(ValidationError):
        LockedRange(start_m=0.0, end_m=10.0, source="api")  # type: ignore[arg-type]


# --- M11.2: and a lock can be taken off again ---------------------------------


def _spans(pad: Scratchpad) -> list[tuple[float, float]]:
    return [(lock.start_m, lock.end_m) for lock in pad.locked]


def test_unlocking_a_whole_lock_removes_it() -> None:
    pad = _pad()
    pad.lock(1000.0, 2000.0)
    freed = pad.unlock(900.0, 2100.0)

    assert _spans(pad) == []
    assert [(f.start_m, f.end_m) for f in freed] == [(1000.0, 2000.0)]


def test_unlocking_the_middle_of_a_lock_leaves_the_ends_locked() -> None:
    """A runner who frees 2-3 km of a 0-10 km lock has said nothing about the other nine.

    Dropping the whole entry would reopen them to the next round's reroute, which is the
    failure scope 8.4's auto-lock exists to prevent, arriving through the undo button.
    """
    pad = _pad()
    pad.lock(0.0, 10_000.0, reason="my usual out-and-back")
    pad.unlock(2000.0, 3000.0)

    assert _spans(pad) == [(0.0, 2000.0), (3000.0, 10_000.0)]
    assert {lock.reason for lock in pad.locked} == {"my usual out-and-back"}


def test_a_trimmed_lock_keeps_the_author_it_had() -> None:
    """Still that lock: narrowing a range the loop wrote does not make it the runner's."""
    pad = _pad()
    pad.lock(0.0, 1000.0, reason="round 1: rerouted", source="loop")
    pad.unlock(500.0, 2000.0)

    assert _spans(pad) == [(0.0, 500.0)]
    assert pad.locked[0].source == "loop"


def test_unlocking_what_i_locked_leaves_the_loops_own_reroute_standing() -> None:
    """The gesture M11.1 exists for, and the whole reason `source` is on the model."""
    pad = _pad()
    pad.lock(0.0, 1000.0, reason="I always run this bit")
    pad.lock(500.0, 1500.0, reason="round 1: rerouted", source="loop")

    freed = pad.unlock(0.0, 2000.0, source="user")

    assert _spans(pad) == [(500.0, 1500.0)]
    assert [f.source for f in freed] == ["user"]


def test_a_lock_that_does_not_overlap_is_left_alone() -> None:
    pad = _pad()
    pad.lock(0.0, 1000.0)
    assert pad.unlock(1000.0, 2000.0) == []
    assert _spans(pad) == [(0.0, 1000.0)]


def test_unlocking_nothing_is_an_error_rather_than_a_silent_no_op() -> None:
    """The same rule `LockedRange` applies to a lock: a zero-length range is a mistake."""
    pad = _pad()
    with pytest.raises(ValueError, match="positive length"):
        pad.unlock(1000.0, 1000.0)


def test_an_unlocked_range_stops_excluding_its_segments() -> None:
    """The point of all of it: `locked_ids` is what keeps a span out of the worst-N."""
    from longrun.core.geo.segments import segment_route

    pad = _pad()
    route = _straight_route()
    pad.route = route
    pad.segments = segment_route(route)
    pad.lock(0.0, route.length_m)
    assert pad.locked_ids(), "the lock covers the route"

    pad.unlock(0.0, route.length_m)
    assert pad.locked_ids() == frozenset()


# --- M11.4: a stored request gets an owner ------------------------------------


def test_a_via_point_lands_on_the_request_the_loop_actually_reads() -> None:
    """`agent.loop` builds `[request.start, *request.via, request.end]`, so a via anywhere
    else is a via the next round will not route through. That is why the owner is the
    scratchpad's request and not a caller's copy of it."""
    pad = _pad()
    pad.route = _straight_route()
    pad.add_via(LatLon(lat=37.77, lon=-122.4155))

    assert [(p.lat, p.lon) for p in pad.request.via] == [(37.77, -122.4155)]


def test_a_via_is_inserted_where_it_falls_along_the_line_not_appended() -> None:
    """`via` is ordered and the router draws through it in order, so appending a point that
    belongs at 300 m to a list whose last entry is at 900 m asks for an out-and-back."""
    pad = _pad()
    route = _straight_route()
    pad.route = route
    far = LatLon(lat=37.77, lon=route.points[9].lon)
    near = LatLon(lat=37.77, lon=route.points[3].lon)

    pad.add_via(far)
    index = pad.add_via(near)

    assert index == 0
    assert [p.lon for p in pad.request.via] == [near.lon, far.lon]


def test_a_via_added_before_there_is_a_line_is_appended_in_the_order_given() -> None:
    """Generate mode: the runner is still naming points, and the order they name them in is
    the order they meant. There is nothing to project onto and nothing to guess."""
    pad = _pad()
    pad.add_via(LatLon(lat=37.79, lon=-122.40))
    pad.add_via(LatLon(lat=37.78, lon=-122.41))

    assert [p.lat for p in pad.request.via] == [37.79, 37.78]


def test_adding_a_via_does_not_quietly_throw_away_the_line() -> None:
    """A line that no longer passes through every via is exactly what `gpx_verify` is for.
    Blanking the route here would destroy the geometry the edit is meant to refine, before
    anything has been drawn to replace it."""
    pad = _pad()
    route = _straight_route()
    pad.route = route
    pad.add_via(LatLon(lat=37.78, lon=-122.415))

    assert pad.route is route


def test_a_via_can_be_placed_at_a_distance_the_caller_names() -> None:
    """The map gesture gives a position; a CLI gives a kilometre mark. Both are answers."""
    pad = _pad()
    route = _straight_route()
    pad.route = route
    pad.add_via(LatLon(lat=37.77, lon=route.points[9].lon))
    index = pad.add_via(LatLon(lat=37.80, lon=-122.30), at_m=50.0)

    assert index == 0, "placed by the distance given, not by where the point itself is"


# --- M11.6: the start window reaches the sweep --------------------------------


def test_a_start_window_is_read_off_the_command_line() -> None:
    from datetime import time

    from longrun.cli.plan import _window

    assert _window("05:30-09:00") == TimeWindow(earliest=time(5, 30), latest=time(9))


def test_no_start_window_is_not_an_all_day_one() -> None:
    """ "I have not fixed an hour" and "any hour" are different requests, and a scorer that
    could not tell them apart would sweep forty-eight candidates for a runner who simply
    named a time."""
    from longrun.cli.plan import _window

    assert _window(None) is None
    assert _window("") is None


def test_an_unreadable_start_window_exits_rather_than_being_ignored() -> None:
    """The failure of ignoring it is silent: the plan is scored at the default hour and the
    sweep answers a question about a window nobody gave."""
    import typer

    from longrun.cli.plan import _window

    with pytest.raises(typer.Exit):
        _window("half past five until nine")
    with pytest.raises(typer.Exit):
        _window("09:00-05:00")


def test_a_request_may_not_carry_a_start_time_and_a_window_at_once() -> None:
    """Scope 6.1 offers one or the other. `PlanRequest` has refused both since M1, which is
    why `longrun plan` drops the start time when a window is given rather than keeping the
    default 07:00 and quietly contradicting it."""
    from datetime import time

    with pytest.raises(ValidationError, match="not both"):
        PlanRequest(
            date=date(2026, 3, 15),
            start=LatLon(lat=37.77, lon=-122.42),
            end=LatLon(lat=37.79, lon=-122.40),
            start_time=time(7),
            start_window=TimeWindow(earliest=time(5), latest=time(9)),
        )
