"""Changing a line that already exists (scope 7.8, 10.3).

Scope 10.3's direct manipulation is five gestures, and three of them are writes to a
*request* - lock, unlock, add a via - which `Scratchpad` owns because the loop reads its
waypoints and its locks from there. The other two change the **line**, and this is where
they live: choosing an alternative for a flagged stretch, and redrawing the whole thing
after the request has moved.

Both produce a `Route` with `source="edited"`, which until M11 was a literal declared in
`core/models/geometry.py` and written by nothing in `src/`. A field with no producer is a
field every reader is free to be wrong about, and `route_diff`, `gpx_verify` check 10 and
the plan sheet all have a reason to ask.

Nothing here scores, and nothing here opens a context. A route is produced; what to do
about it is `core.plan.refresh.rescore_plan`'s question and the CLI's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from longrun.core.geo.gpx import normalize
from longrun.core.models.geometry import LatLon, Route

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.plan.scratchpad import Scratchpad
    from longrun.core.routing.base import CostingModel, Router


class NotAnEdit(ValueError):
    """An edit was asked for that would leave the line unchanged, or leave it in pieces."""


def splice(route: Route, start_m: float, end_m: float, replacement: Route) -> Route:
    """The line, with `start_m`-`end_m` replaced by `replacement`. Scope 10.3's "choose an
    alternative", which auto-locks (scope 7.8) once the caller has the new line.

    **The head and the tail keep their elevations and the replacement has none**, and that
    is the whole reason this returns a route rather than a list of points. Scope 7.1 says
    elevation comes from the terrain model, and the terrain under the untouched two-thirds
    of a route has not changed - so re-reading a DEM for it would be work at best and, on a
    machine with no rasters, would replace a good profile with nulls. The replaced stretch
    reads as unknown until something samples it, which `ElevationProfile.samples_missing`
    already counts and reports.

    **The join is the caller's to get right.** Nothing here checks that `replacement` starts
    and ends near the cut, because the honest threshold for "near" is the router's, not
    this function's: an alternative asked for between two points on the line connects by
    construction, and a line spliced with something unrelated is what `gpx_verify` exists to
    catch. What is refused is the pair of mistakes that produce no line at all - a cut with
    no length, and one that consumes an end of the route.
    """
    if end_m <= start_m:
        raise NotAnEdit("a spliced range must have positive length")
    head = [p for p in route.points if p.cum_dist_m < start_m]
    tail = [p for p in route.points if p.cum_dist_m > end_m]
    if not head or not tail:
        raise NotAnEdit(
            f"splicing {start_m:.0f}-{end_m:.0f} m of a {route.length_m:.0f} m route would "
            f"consume one of its ends; re-route instead of splicing"
        )
    coordinates = (
        [(p.lat, p.lon, p.ele_m) for p in head]
        + [(p.lat, p.lon, None) for p in replacement.points]
        + [(p.lat, p.lon, p.ele_m) for p in tail]
    )
    return Route(
        id=route.id,
        points=normalize(coordinates),
        name=route.name,
        source="edited",
    )


def redraw(
    pad: Scratchpad,
    router: Router,
    *,
    custom_model: CostingModel | None = None,
) -> Route:
    """The request's current waypoints, drawn again. Scope 10.3's add-a-via and avoid-polygon.

    The waypoint list is built exactly as `agent.loop` builds it -
    `[request.start, *request.via, request.end]` - because a redraw that used a different
    list would produce a line the loop would not reproduce on its next round, and the two
    would then disagree about what the plan is.

    **The plan's own policy, not a fresh one.** `RoutingPolicy` is frozen and resolved once,
    persisted specifically so a resume in another process cannot compute a different one
    (`core/models/routing.py`). A redraw is exactly that other process, so it carries
    `pad.policy.avoid_polygons` and the stored costing model rather than resolving either
    again. Where a gesture *adds* an avoid polygon, the honest thing is a re-route of the
    whole line - which is what this is - and not a patch to a policy the first half of the
    line was already drawn without.
    """
    request = pad.request
    if request.start is None or request.end is None:
        raise NotAnEdit("a redraw needs a start and an end; this request has neither")
    waypoints: list[LatLon] = [request.start, *request.via, request.end]
    # The plan's own stored model rather than one rebuilt from the profile, for the reason
    # the policy is persisted at all: two resolutions of one plan may differ, and the line
    # this one replaces was drawn with the first.
    drawn = router.route(
        waypoints,
        avoid_polygons=list(pad.policy.avoid_polygons) or None,
        custom_model=custom_model if custom_model is not None else pad.policy.custom_model,
    )
    return drawn.model_copy(
        update={"id": pad.route.id if pad.route else drawn.id, "source": "edited"}
    )


__all__ = ["NotAnEdit", "redraw", "splice"]
