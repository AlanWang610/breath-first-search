"""Changing a plan that already exists (scope 7.8, 10.3).

Scope 10.3's direct manipulation is five gestures. Three are writes to a *request* - lock,
unlock, add a via - and two change the **line**: choosing an alternative for a flagged
stretch, and redrawing the whole thing after the request has moved.

**All five live here as functions over plain lists**, because a request has two homes and
both of them need the same answer. `Scratchpad.locked` is the loop's live list and
`PlanRequest.locked` is what the runner asked for and what `gpx_verify` check 10 reads;
`agent.loop._with_locks` exists because the two drift. A second implementation would drift
in the same way and in a place nobody is watching, so `Scratchpad` delegates here and the
CLI operates on a stored plan's request through the same three functions.

Both produce a `Route` with `source="edited"`, which until M11 was a literal declared in
`core/models/geometry.py` and written by nothing in `src/`. A field with no producer is a
field every reader is free to be wrong about, and `route_diff`, `gpx_verify` check 10 and
the plan sheet all have a reason to ask.

Nothing here scores, and nothing here opens a context. A route is produced; what to do
about it is `core.plan.refresh.rescore_plan`'s question and the CLI's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from longrun.core.geo.gpx import haversine_m, normalize
from longrun.core.models.geometry import LatLon, Route
from longrun.core.models.request import LockedRange, LockSource

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.plan.scratchpad import Scratchpad
    from longrun.core.routing.base import CostingModel, Router


class NotAnEdit(ValueError):
    """An edit was asked for that would leave the line unchanged, or leave it in pieces."""


def lock_range(
    locked: Sequence[LockedRange],
    start_m: float,
    end_m: float,
    reason: str | None = None,
    *,
    source: LockSource = "user",
) -> list[LockedRange]:
    """`locked` with one more range on it (scope 6.4).

    **Appends, and deliberately does not merge.** Two overlapping locks and their union
    exclude exactly the same segments - `locked_segment_ids` is a union over ranges - so
    merging changes nothing the loop can see, and it *does* change something a reader can:
    the golden expectation records `loop.locked` verbatim, entry by entry, which is how it
    notices a lock that stopped being applied. Collapsing the list would move that without
    changing a measurement. `unlock_range` is where ranges are taken apart, and it handles
    overlaps because it has to.
    """
    return [*locked, LockedRange(start_m=start_m, end_m=end_m, reason=reason, source=source)]


def unlock_range(
    locked: Sequence[LockedRange],
    start_m: float,
    end_m: float,
    *,
    source: LockSource | None = None,
) -> tuple[list[LockedRange], list[LockedRange]]:
    """`locked` with `start_m`-`end_m` released, and the locks that release took apart.

    **A partial overlap is trimmed, not dropped.** A runner who frees 2-3 km of a lock
    spanning 0-10 km has said nothing about the other nine, and removing the whole entry
    would reopen them to the next round's reroute - which is the failure scope 8.4's
    auto-lock exists to prevent, arriving through the undo button. Each surviving piece
    keeps the original `reason` and `source`: narrowing a range the loop wrote does not
    make it the runner's.

    `source` selects whose locks may be taken apart, which is what `LockedRange.source`
    exists for. `None` means any, the honest reading of "free this range"; the UI's "unlock
    what I locked" passes `"user"` and leaves the loop's own reroute locks standing.

    Returns `(kept, freed)`, with `freed` holding the locks **as they were before**, so a
    caller with a terminal reports what it released rather than a count of nothing.
    """
    if end_m <= start_m:
        raise NotAnEdit("unlocked range must have positive length")

    kept: list[LockedRange] = []
    freed: list[LockedRange] = []
    for lock in locked:
        selected = source is None or lock.source == source
        if not selected or lock.end_m <= start_m or lock.start_m >= end_m:
            kept.append(lock)
            continue
        freed.append(lock)
        # Up to two survivors: the head before the released range and the tail after it.
        # Both, for a lock the range falls strictly inside.
        if lock.start_m < start_m:
            kept.append(lock.model_copy(update={"end_m": start_m}))
        if lock.end_m > end_m:
            kept.append(lock.model_copy(update={"start_m": end_m}))
    return kept, freed


def insert_via(
    via: Sequence[LatLon], point: LatLon, route: Route | None, at_m: float | None = None
) -> tuple[list[LatLon], int]:
    """`via` with `point` added where it falls along `route`, and the index it went in at.

    **In route order, not appended.** `via` is an ordered list and `agent.loop` draws
    through it in that order, so appending a point that belongs at 3 km to a list whose last
    entry is at 30 km asks for a route that runs out and back.

    With no route there is nothing to order against and it appends: that is generate mode,
    where the runner is still naming points and the order they name them in is the order
    they meant.
    """
    where = at_m if at_m is not None else distance_along(route, point)
    out = list(via)
    if route is None or where is None:
        out.append(point)
        return out, len(out) - 1
    index = sum(1 for each in out if (distance_along(route, each) or 0.0) <= where)
    out.insert(index, point)
    return out, index


def distance_along(route: Route | None, point: LatLon) -> float | None:
    """Where a point sits along a line, to the nearest sampled point.

    The rule `start_time_optimizer._eta_at` already uses, and for the same reason: the
    question is which kilometre mark a place is at, and route points are far finer than that
    answer needs to be. `None` when there is no line to ask about - which is not zero, since
    zero is the start.
    """
    if route is None:
        return None
    best: float | None = None
    at = 0.0
    for candidate in route.points:
        gap = haversine_m(point.lat, point.lon, candidate.lat, candidate.lon)
        if best is None or gap < best:
            best, at = gap, candidate.cum_dist_m
    return at


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

    **The stored costing model, and the union of the stored and current avoid areas.** Those
    two halves of `RoutingPolicy` are treated differently on purpose. The costing model is
    what "frozen and resolved once" protects (`core/models/routing.py`): it is persisted
    precisely so a resume in another process cannot compute a different one, and a redraw is
    exactly that other process, so it uses the stored one rather than rebuilding it from a
    profile that may have moved. The avoid areas are the thing the runner has just changed -
    `request.avoid_polygons` is where a drawn polygon lands - and a redraw that ignored them
    would honour the gesture by doing nothing.

    That is not a patch to the policy, and the distinction is the one M12's own note draws:
    a policy applies to *a line*, and this draws a whole new line under one set of areas
    rather than leaving half of it drawn without them. Deduplicated on the serialised area
    so that a polygon already resolved into the policy is not sent twice.
    """
    import json

    request = pad.request
    if request.start is None or request.end is None:
        raise NotAnEdit("a redraw needs a start and an end; this request has neither")
    waypoints: list[LatLon] = [request.start, *request.via, request.end]

    areas: list[dict[str, Any]] = []
    seen: set[str] = set()
    for area in [*pad.policy.avoid_polygons, *request.avoid_polygons]:
        key = json.dumps(area, sort_keys=True)
        if key not in seen:
            seen.add(key)
            areas.append(area)

    drawn = router.route(
        waypoints,
        avoid_polygons=areas or None,
        custom_model=custom_model if custom_model is not None else pad.policy.custom_model,
    )
    return drawn.model_copy(
        update={"id": pad.route.id if pad.route else drawn.id, "source": "edited"}
    )


__all__ = [
    "NotAnEdit",
    "distance_along",
    "insert_via",
    "lock_range",
    "redraw",
    "splice",
    "unlock_range",
]
