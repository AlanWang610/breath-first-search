"""The router interface (scope 3.8, 4.3, 6.4).

`core/` exposes `route`, `alternatives` and `map_match`, and the costing model is an
opaque object the adapter owns. Nothing above this module knows GraphHopper exists, which
is what lets the router be swapped without touching a scorer or the agent.

`NoRouteError` deserves its shape. Scope 6.4 asks for a *graceful* no-route: pedestrian
graphs disconnect at bridges and interchanges far more often than people expect, and
"could not route" is useless where "could not join via-point 3 to via-point 4, nearest
connected point is 240 m away" is actionable.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from longrun.core.models.geometry import LatLon, Route

#: The costing model is opaque above the adapter: a dict here, a GraphHopper custom model
#: in the adapter, something else entirely under a different router.
CostingModel = dict[str, Any]


class RoutingError(RuntimeError):
    """Base for every routing failure."""


class RouterUnavailable(RoutingError):
    """No router is configured or reachable.

    Distinct from a routing failure: repair mode (scope 6.1) never needs a router, so
    this must degrade to a coverage entry rather than fail a plan that did not want one.
    """


class NoRouteError(RoutingError):
    """No path exists between two points, reported usefully (scope 6.4)."""

    def __init__(
        self,
        start: LatLon,
        end: LatLon,
        nearest_connected_m: float | None = None,
        detail: str | None = None,
    ) -> None:
        where = f"({start.lat:.5f},{start.lon:.5f}) -> ({end.lat:.5f},{end.lon:.5f})"
        near = (
            f"; nearest connected point {nearest_connected_m:.0f} m away"
            if nearest_connected_m is not None
            else ""
        )
        super().__init__(f"no pedestrian route for {where}{near}{f': {detail}' if detail else ''}")
        self.start = start
        self.end = end
        self.nearest_connected_m = nearest_connected_m


@runtime_checkable
class Router(Protocol):
    """What `core/` may ask of a routing engine."""

    def route(
        self,
        waypoints: list[LatLon],
        profile: str = "foot",
        avoid_polygons: list[dict[str, Any]] | None = None,
        custom_model: CostingModel | None = None,
    ) -> Route: ...

    def alternatives(self, gpx: Route, segment_idx: int, k: int = 3) -> list[Route]: ...

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]: ...


class NullRouter:
    """A router that is not there, failing loudly and specifically.

    Lets repair mode (scope 6.1) run end to end with no routing engine configured, while
    making any attempt to *generate* a route a clear error rather than a confusing empty
    result. `alternatives` returns nothing, which degrades scope 8.1 step 6 from
    "flag and fix" to "flag" - an honest diagnostic rather than a silent non-improvement.
    """

    name = "null"

    def route(
        self,
        waypoints: list[LatLon],
        profile: str = "foot",
        avoid_polygons: list[dict[str, Any]] | None = None,
        custom_model: CostingModel | None = None,
    ) -> Route:
        raise RouterUnavailable(
            "no router configured: generate mode needs one, repair mode does not"
        )

    def alternatives(self, gpx: Route, segment_idx: int, k: int = 3) -> list[Route]:
        return []

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]:
        """Return the track unchanged, with no way ids.

        Not an error: an unmatched route is still scoreable from geometry alone, and the
        missing way ids surface as lowered confidence in the scorers that wanted them.
        """
        return track, [None] * len(track.points)
