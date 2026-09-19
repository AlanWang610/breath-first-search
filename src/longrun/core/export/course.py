"""What the course writers share, and the rule that keeps them honest (scope 9, 10.1).

Two positioning rules, and this module exists so neither can be got wrong in one format and
right in the other:

* **A course point sits at the route position for its `cum_dist_m`.** A bailout 6 km off the
  line would otherwise put a course point 6 km off the track, and a device would route the
  runner to it. `at_distance` is the only way to ask that question.
* **The GPX keeps the true position.** `gpx_write` reads `waypoint.position` and never calls
  `at_distance`; `fit.py` and `tcx.py` call `at_distance` and never read `position`. One
  function each way, and a test asserts both directions - if only one existed, the wrong one
  could pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from longrun.core.models.geometry import LatLon, Route
from longrun.core.models.waypoint import PlanWaypoint, WaypointKind

#: Course points a device will accept before it starts dropping them. Garmin's documented
#: limit varies by model and this is the commonly safe floor; it is applied in the writers
#: only, so `Plan.waypoints` and the GPX keep everything.
DEVICE_COURSE_POINT_MAX = 200

#: Which kinds survive when there are more waypoints than a device will take. Hazards and
#: gates are never dropped: one is a safety finding and the other is a locked gate, and both
#: are things a runner needs at the moment they reach them.
NEVER_DROP: frozenset[WaypointKind] = frozenset({"hazard", "gate"})

#: Order of sacrifice for the rest. Water before food because you can finish a run hungry.
KIND_PRIORITY: tuple[WaypointKind, ...] = (
    "water",
    "toilet",
    "bailout",
    "crew",
    "food",
    "marker",
)

#: Short prefixes for a course point's name. Keyed on *kind* rather than on the target
#: format's type, which is what lets a kind that degrades to a generic type keep its identity
#: in the name - see `tcx.py`.
NAME_PREFIX: dict[WaypointKind, str] = {
    "water": "H2O",
    "toilet": "WC",
    "food": "FOOD",
    "bailout": "BAIL",
    "hazard": "HAZ",
    "gate": "GATE",
    "crew": "CREW",
    "marker": "KM",
}


class ExportError(ValueError):
    """A course could not be written from this plan, and why."""


def at_distance(
    route: Route, etas: Sequence[datetime], cum_m: float
) -> tuple[LatLon, datetime | None, float]:
    """The route point, arrival and exact distance at a distance along the route.

    The single answer to "where on the track is this". Both writers call it and neither
    reads `PlanWaypoint.position`, which is what keeps a course point on the line.
    """
    if not route.points:
        raise ExportError("this plan's route has no points, so there is nothing to place")
    best = 0
    for index, point in enumerate(route.points):
        if abs(point.cum_dist_m - cum_m) < abs(route.points[best].cum_dist_m - cum_m):
            best = index
    point = route.points[best]
    arrival = etas[min(best, len(etas) - 1)] if etas else None
    return LatLon(lat=point.lat, lon=point.lon), arrival, point.cum_dist_m


def short_name(kind: WaypointKind, ordinal: int, limit: int = 10) -> str:
    """A course point's name: `H2O3`, `WC2`, `BAIL12`.

    Short because TCX caps `CoursePoint/Name` at ten characters and a longer value fails XSD
    validation outright - Garmin Connect rejects the upload rather than truncating. Unique
    because a course with three points all named WATER is legal and unreadable; the ordinal
    is what makes "the second water stop" sayable. The human label goes in the notes, which
    are not capped.
    """
    prefix = NAME_PREFIX.get(kind, kind.upper()[:4])
    name = f"{prefix}{ordinal}"
    return name[:limit]


def select_for_device(
    waypoints: Sequence[PlanWaypoint],
    route_length_m: float,
    limit: int = DEVICE_COURSE_POINT_MAX,
) -> tuple[list[PlanWaypoint], list[str]]:
    """At most `limit` course points, chosen so the survivors span the whole route.

    Thinning by along-route spacing rather than truncating the tail. Truncation is the
    tempting implementation and it produces a course with every café in the first three
    kilometres and nothing after - which looks like it worked, which is the worst kind of
    wrong for a file somebody follows in the dark.
    """
    ordered = sorted(waypoints, key=lambda w: (w.cum_dist_m, w.kind, w.label))
    if len(ordered) <= limit:
        return list(ordered), []

    kept: list[PlanWaypoint] = [w for w in ordered if w.kind in NEVER_DROP]
    budget = limit - len(kept)
    dropped: dict[str, int] = {}

    for kind in KIND_PRIORITY:
        of_kind = [w for w in ordered if w.kind == kind]
        if not of_kind:
            continue
        if budget <= 0:
            dropped[kind] = dropped.get(kind, 0) + len(of_kind)
            continue
        # A share of the remaining budget, then thinned by spacing within it.
        share = max(1, min(len(of_kind), budget))
        spacing = route_length_m / share if share else route_length_m
        picked: list[PlanWaypoint] = []
        last = -spacing
        for waypoint in of_kind:
            if len(picked) >= share:
                break
            if waypoint.cum_dist_m - last >= spacing:
                picked.append(waypoint)
                last = waypoint.cum_dist_m
        # Spacing can under-fill on a clustered route; top up in order so the budget is used.
        if len(picked) < share:
            for waypoint in of_kind:
                if len(picked) >= share:
                    break
                if waypoint not in picked:
                    picked.append(waypoint)
        kept.extend(picked)
        budget -= len(picked)
        if len(of_kind) > len(picked):
            dropped[kind] = dropped.get(kind, 0) + (len(of_kind) - len(picked))

    kept.sort(key=lambda w: (w.cum_dist_m, w.kind, w.label))
    notes: list[str] = []
    if dropped:
        detail = ", ".join(f"{count} {kind}" for kind, count in sorted(dropped.items()))
        notes.append(
            f"{sum(dropped.values())} course point(s) dropped for the {limit}-point device "
            f"limit: {detail}. The GPX and plan.json keep all {len(ordered)}."
        )
    return kept, notes


def require_etas(route: Route, etas: Sequence[datetime]) -> None:
    """Both course formats need a timestamp per point, so refuse rather than invent one.

    A FIT record and a TCX `CoursePoint` both require a time. A plan whose pacing failed has
    no ETA vector, and fabricating one would put a course on a watch that claims a schedule
    nobody computed.
    """
    if not etas:
        raise ExportError(
            "this plan has no ETA vector, so a course would need invented timestamps; "
            "re-score it before exporting"
        )
    if len(etas) != len(route.points):
        raise ExportError(
            f"this plan has {len(etas)} ETAs for {len(route.points)} route points, so the "
            f"two do not describe the same line"
        )


__all__ = [
    "DEVICE_COURSE_POINT_MAX",
    "KIND_PRIORITY",
    "NAME_PREFIX",
    "NEVER_DROP",
    "ExportError",
    "at_distance",
    "require_etas",
    "select_for_device",
    "short_name",
]
