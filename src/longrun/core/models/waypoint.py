"""A place beside the route that a scorer found (scope 9).

Scope 9's first listed output is a GPX "with waypoints for water, toilets, bailouts, hazards
and gates". `core/geo/gpx.py::gpx_write` has taken waypoints since M1 and no caller has ever
passed any, because the scorers that find these places throw the positions away:
`services.py` holds a fountain's latitude and longitude, calls `frame.locate`, and keeps a
scalar distance; `bailouts.py` finds the winning transit stop and returns a float;
`hazards.py` has the exact point where the route crosses a railway and keeps a count, with
the position surviving only as prose inside `Flag.detail`.

`SegmentMeasurement.values` is scalars-only and frozen, and deliberately so - that is what
keeps the plan sheet renderable and the golden content hash meaningful. So a waypoint is not
a measurement. It hangs off `ScorerResult` as a fourth list beside `coverage`, which means
`measurements_sha256` does not see it and no golden hash moves for a position.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from longrun.core.models.geometry import LatLon, Meters

#: The eight kinds scope 9 names, plus `marker` and `crew`. Defined here rather than in
#: `core/geo/gpx.py`, where it lived from M1 to M10 with no consumer outside that file: the
#: models layer may not import the geo layer, and `gpx.py` re-exports it so `gpx_write`'s
#: signature does not move.
WaypointKind = Literal["water", "toilet", "food", "bailout", "hazard", "gate", "marker", "crew"]


class PlanWaypoint(BaseModel):
    """A place a scorer found, and where it is.

    Note the other meaning of "waypoint" in this codebase: `cli/plan.py`, `regions/routers.py`
    and `core/routing/base.py` use it for the *router input points* a route is drawn through,
    which `PlanRequest` calls `start`, `end` and `via`. This is the other thing - a point of
    interest beside the line, not a point the line must pass through.
    """

    model_config = ConfigDict(frozen=True)

    #: Where the thing actually is. The GPX writes this and nothing else.
    position: LatLon
    kind: WaypointKind
    #: Human text, unbounded here. The course writers truncate and say that they did - TCX
    #: caps a course point's name at ten characters, which is a property of that format
    #: rather than of the fountain.
    label: str
    #: Distance along the route. This is what a *course* point is placed at, and the two are
    #: deliberately different: a bailout 6 km off the line belongs at the point on the track
    #: where you would leave it, not at the station.
    cum_dist_m: Meters
    #: Which scorer found it, so a plan can say why it is here.
    scorer: str
    #: How far `position` is from the line, when the scorer measured it.
    offset_m: float | None = None
    #: Arrival at `cum_dist_m`. A real datetime rather than `crew_points`' "%H:%M" string,
    #: which exists only because `SegmentMeasurement.values` holds scalars; a sibling list
    #: has no such constraint, and both course formats need a timestamp.
    eta: datetime | None = None
    #: Prose for the sheet - opening hours, the stop's name, why this is a hazard. Never
    #: asserted by a golden.
    detail: str | None = None


__all__ = ["PlanWaypoint", "WaypointKind"]
