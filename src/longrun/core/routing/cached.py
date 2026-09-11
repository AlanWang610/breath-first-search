"""A router that answers from the cassette, and is charged for the calls it makes.

Two separate jobs that the first draft of this milestone fused, and they are worth keeping
apart because only one of them needs a cache.

**Metering.** `grep Budget core/routing/` returned nothing before M5.4: no router call had
ever been charged to a budget, so scope 6.4's 200-call cap counted forecasts and adapters
and not the calls scope 8.1 step 6 makes most of - up to five rounds of them. That is one
line, inside the producer, so a replay costs nothing.

**Replay.** Without it no golden can exercise the loop, because the loop's whole subject is
what comes back from an alternative. `LONGRUN_OFFLINE=1` then makes a missing recording a
loud `CacheMiss` rather than a quiet network call.

Three rules, each of which fails silently if broken:

* **What is cached is the raw path dicts**, exactly as the server sent them, with
  `path_to_route` left as the single decoder. `SqliteCache.put` stores
  `json.dumps(value, default=str)`, so handing it a pydantic `Route` would store the
  model's *repr* and return a `str` on the next hit - which a scorer's blanket handler
  would report as "scorer failed" three layers away. Same argument as M4's: cache the feed
  payload, parse outside the cache.
* **The key is the graph, not the day.** `STATIC_DAY` is the convention `forecast.py`
  already uses for a date-free lookup, and a route is not a forecast. What a route *does*
  depend on is the graph it was drawn on, and scope 7 rebuilds that weekly - so the graph
  identity goes in the args. This is a new exception to the `(tool, args, date)` rule and
  not an extension of ADR 0007's: that one rests on a COG's bytes being content-addressed
  and immutable, and a route is neither.
* **A degraded empty is never stored.** `alternatives` swallows `RouterUnavailable` and
  `NoRouteError` into `[]`, and `[]` is perfectly cacheable - so one outage during a
  recording session would pin "no alternatives here" into a cassette for good. Only the
  inner router's *answers* are recorded; its failures are not.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from longrun.core.data.cache import COORD_PRECISION, STATIC_DAY, fetch
from longrun.core.models.geometry import LatLon, Route
from longrun.core.routing.base import CostingModel, NoRouteError, RouterUnavailable
from longrun.core.routing.detour import detour_area, detour_waypoints
from longrun.core.routing.graphhopper import FOOT_PROFILE, path_to_route, route_body

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.data.base import Cache
    from longrun.core.models.context import Budget


@runtime_checkable
class PathRouter(Protocol):
    """What this wrapper needs of the engine underneath it.

    Narrower than `Router` and deliberately so: what is worth caching is the **raw** answer
    and the single decoder above it, so the wrapper talks to `paths` rather than to
    `route`. Stated as a protocol rather than typed as `Router` because `Router` does not
    declare `paths` - it is a transport detail, and a `type: ignore` in its place would
    have hidden the requirement rather than recorded it.
    """

    def paths(self, body: dict[str, Any], waypoints: Sequence[LatLon]) -> list[dict[str, Any]]: ...

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]: ...


#: Tool names are provider-scoped and dotted, as `nws.points` and `open_meteo.forecast`
#: are. One neutral `route` key would collide across engines the first time a second one
#: appeared, and the cassette would answer a question it was never asked.
ROUTE_TOOL = "graphhopper.route"
MATCH_TOOL = "graphhopper.map_match"


def _rounded(waypoints: list[LatLon]) -> list[list[float]]:
    """Coordinates as they go into a cache key.

    `args_hash` does no rounding of its own - every caller does it, and `open_meteo_args`
    is the precedent. Without this a route requested from a point recomputed by floating
    point arithmetic misses a cassette it is identical to.
    """
    return [
        [round(point.lon, COORD_PRECISION), round(point.lat, COORD_PRECISION)]
        for point in waypoints
    ]


class CachedRouter:
    """Wraps any `Router`, recording what it answered and charging what it cost."""

    name = "cached"

    def __init__(
        self,
        inner: PathRouter,
        cache: Cache,
        budget: Budget,
        *,
        graph: str | None = None,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.budget = budget
        #: The graph identity, from `SnapshotPins`. A route is a function of the graph it
        #: was drawn on, so two graphs must not share a key. `None` records "unpinned",
        #: which is honest and still distinct from any pinned value.
        self.graph = graph

    # -- the three protocol methods -------------------------------------------

    def route(
        self,
        waypoints: list[LatLon],
        profile: str = FOOT_PROFILE,
        avoid_polygons: list[dict[str, Any]] | None = None,
        custom_model: CostingModel | None = None,
    ) -> Route:
        body = route_body(waypoints, profile, avoid_polygons, custom_model)
        paths = self._paths(body, waypoints, lambda: self.inner_paths(body, waypoints))
        return path_to_route(paths[0], route_id="generated")

    def alternatives(
        self,
        gpx: Route,
        around: tuple[float, float] | None = None,
        k: int = 3,
        *,
        profile: str = FOOT_PROFILE,
        avoid_polygons: list[dict[str, Any]] | None = None,
        custom_model: CostingModel | None = None,
    ) -> list[Route]:
        """Recorded like any other answer - but a *failure* is never recorded.

        The inner router turns `RouterUnavailable` and `NoRouteError` into an empty list,
        which is indistinguishable from "this span genuinely has no detour". Calling the
        inner method directly here, rather than reusing its swallow, is what keeps an
        outage out of the cassette.
        """
        if around is None:
            waypoints = [
                LatLon(lat=gpx.points[0].lat, lon=gpx.points[0].lon),
                LatLon(lat=gpx.points[-1].lat, lon=gpx.points[-1].lon),
            ]
            areas = list(avoid_polygons or [])
        else:
            waypoints = detour_waypoints(gpx, *around)
            areas = [*(avoid_polygons or []), detour_area(gpx, *around)]

        body = route_body(
            waypoints,
            profile=profile,
            avoid_polygons=areas or None,
            custom_model=custom_model,
            alternatives=k if len(waypoints) == 2 else 0,
        )
        try:
            paths = self._paths(body, waypoints, lambda: self.inner_paths(body, waypoints))
        except (RouterUnavailable, NoRouteError):
            return []
        return [path_to_route(path, route_id=f"alt-{i}") for i, path in enumerate(paths)]

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]:
        """Matching is cached on the track's geometry, not on its GPX serialisation.

        `map_match` posts a GPX document, and keying on that string would make the cassette
        sensitive to whitespace and to float formatting - two things that have nothing to
        do with the question being asked.
        """
        args = {"graph": self.graph, "points": _rounded_track(track), "profile": FOOT_PROFILE}

        def producer() -> Any:
            self.budget.spend_api_call()
            matched, way_ids = self.inner.map_match(track)
            return {
                "points": [[p.lon, p.lat] for p in matched.points],
                "way_ids": list(way_ids),
            }

        payload = fetch(self.cache, MATCH_TOOL, args, STATIC_DAY, producer)
        points = payload.get("points") or []
        if len(points) < 2:
            return track, [None] * len(track.points)
        replayed = path_to_route({"points": {"coordinates": points}}, route_id=track.id)
        return replayed, list(payload.get("way_ids") or [None] * len(replayed.points))

    # -- the one door ----------------------------------------------------------

    def inner_paths(self, body: dict[str, Any], waypoints: list[LatLon]) -> list[dict[str, Any]]:
        """Charge the budget, then ask. Spent inside the producer, so a hit is free."""
        self.budget.spend_api_call()
        paths = self.inner.paths(body, waypoints)
        return list(paths)

    def _paths(
        self,
        body: dict[str, Any],
        waypoints: list[LatLon],
        producer: Any,
    ) -> list[dict[str, Any]]:
        args = {
            "graph": self.graph,
            "points": _rounded(waypoints),
            "profile": body.get("profile"),
            "custom_model": body.get("custom_model"),
            "algorithm": body.get("algorithm"),
            "max_paths": body.get("alternative_route.max_paths"),
        }
        payload = fetch(self.cache, ROUTE_TOOL, args, STATIC_DAY, lambda: {"paths": producer()})
        paths = payload.get("paths") or []
        if not paths:
            raise NoRouteError(
                waypoints[0], waypoints[-1], detail="the recorded answer holds no paths"
            )
        return list(paths)


def _rounded_track(track: Route) -> list[list[float]]:
    return [
        [round(point.lon, COORD_PRECISION), round(point.lat, COORD_PRECISION)]
        for point in track.points
    ]


__all__ = ["MATCH_TOOL", "ROUTE_TOOL", "CachedRouter", "PathRouter"]
