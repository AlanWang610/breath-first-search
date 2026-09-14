"""GraphHopper behind the `Router` protocol (scope 4.3, 7.1; ADR 0001).

The only module in `core/` that knows GraphHopper exists. Everything above it sees
`route`, `alternatives` and `map_match`, and a costing model that is an opaque dict.

Four constraints M0.2 confirmed against a running server, all of which shape this file:

**Query-time custom models are `POST /route` only.** Not GET, not `/isochrone`, not
`/spt`, not `/map-matching`. So this adapter is POST-only, which is why there is no
convenience GET path to fall into by accident.

**They need flexible mode.** `ch.disable=true` on every request; the region config
prepares LM rather than CH for exactly this. A custom model sent to a CH-prepared profile
comes back as a 400 that reads like a schema error.

**Avoid-polygons go inline in the body as `areas`.** Named `custom_areas` are an
import-time artefact of the graph, so a plan cannot invent one — §13's build has to know
about them, and a per-plan avoidance has to travel with the request.

**`lts` is an encoded value on the graph, not something this sends.** ADR 0001: the import
writes it, `load()` rebuilds the encoding manager from the graph's own properties, and the
stock jar serves it. This module only *reads* it in a custom model.

`map_match` is a different endpoint with a different content type, and it is what risk R4
turned on: `POST /match` returns real `osm_way_id` values per edge, which is what scope
6.2's accepted-road set is built from. Repair mode does not use it — `core.geo.matching`
snaps geometrically instead — so a server without map-matching enabled degrades rather
than fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.core.data.cache import CacheMiss
from longrun.core.models.geometry import LatLon, Route
from longrun.core.routing.base import CostingModel, NoRouteError, RouterUnavailable
from longrun.core.routing.detour import detour_area, detour_waypoints

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

#: Where a local GraphHopper listens, and the environment variable that moves it.
DEFAULT_URL = "http://localhost:8989"
URL_ENV_VAR = "LONGRUN_GRAPHHOPPER_URL"

#: Seconds. A pedestrian route across a region is well under a second; this is the budget
#: for a server that is starting up or swapping, not for the routing itself.
TIMEOUT_S = 30.0

#: GraphHopper's own profile names, as `deploy/graphhopper/config-bayarea-lts.yml` defines
#: them. `car` exists for `crew_points`' drive times (scope 7.7), not for runners.
FOOT_PROFILE = "foot"
CAR_PROFILE = "car"


def url_from_env(env: dict[str, str] | None = None) -> str:
    import os

    source = os.environ if env is None else env
    return source.get(URL_ENV_VAR, "").strip() or DEFAULT_URL


def route_body(
    waypoints: Sequence[LatLon],
    profile: str = FOOT_PROFILE,
    avoid_polygons: list[dict[str, Any]] | None = None,
    custom_model: CostingModel | None = None,
    alternatives: int = 0,
) -> dict[str, Any]:
    """The POST body for one routing request.

    A named function with its own test because it is the whole contract with the router,
    and because two of its fields are easy to get subtly wrong: `points` is
    **[lon, lat]** where every other coordinate in this codebase is (lat, lon), and
    `ch.disable` has to be present or a custom model is silently ignored on a CH profile.
    """
    body: dict[str, Any] = {
        "profile": profile,
        "points": [[point.lon, point.lat] for point in waypoints],
        "points_encoded": False,
        "instructions": False,
        "elevation": False,
        # Flexible mode. Without it a custom model is either ignored or rejected, and the
        # difference between those two is a routing result that looks fine and is not.
        "ch.disable": True,
    }
    model = dict(custom_model or {})
    if avoid_polygons:
        # `areas` is a GeoJSON FeatureCollection in the body; the custom model refers to
        # its features by id in a `priority` rule.
        model["areas"] = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": polygon.get("id", f"avoid_{index}"),
                    "properties": {},
                    "geometry": polygon.get("geometry", polygon),
                }
                for index, polygon in enumerate(avoid_polygons)
            ],
        }
        model.setdefault("priority", [])
        model["priority"] = list(model["priority"]) + [
            {"if": f"in_{feature['id']}", "multiply_by": "0"}
            for feature in model["areas"]["features"]
        ]
    if model:
        body["custom_model"] = model
    if alternatives > 0:
        body["algorithm"] = "alternative_route"
        body["alternative_route.max_paths"] = alternatives
    return body


def path_to_route(path: dict[str, Any], route_id: str) -> Route:
    """One GraphHopper path as a `Route`, with cumulative distance recomputed.

    Recomputed rather than interpolated from `path["distance"]`: `Route` is the unit every
    scorer keys off, and `cum_dist_m` has to be the distance along *these* points. A
    proportional fill would put segment boundaries in the wrong place on any path whose
    points are unevenly spaced, which is every path a router returns.
    """
    from longrun.core.geo.gpx import densify, normalize

    coordinates = (path.get("points") or {}).get("coordinates") or []
    if len(coordinates) < 2:
        raise NoRouteError(
            LatLon(lat=0.0, lon=0.0),
            LatLon(lat=0.0, lon=0.0),
            detail="the router returned a path with fewer than two points",
        )
    # `normalize` then `densify`, which are opposite corrections to opposite defects and
    # a routed path needs both. A router emits near-coincident points where edges meet -
    # boundaries `segment_route` would split on that mean nothing - and then 133 m of
    # nothing along a straight street, which is coarser than every per-point measurement in
    # the system assumes. Running `normalize` first means the deduplication sees the
    # router's own points rather than the ones interpolation just invented.
    points = densify(normalize([(float(c[1]), float(c[0]), None) for c in coordinates]))
    if len(points) < 2:
        raise NoRouteError(
            LatLon(lat=float(coordinates[0][1]), lon=float(coordinates[0][0])),
            LatLon(lat=float(coordinates[-1][1]), lon=float(coordinates[-1][0])),
            detail="the router returned a path with no length",
        )
    return Route(id=route_id, points=points)


class GraphHopperRouter:
    """`Router` over a GraphHopper server. POST only, flexible mode only."""

    name = "graphhopper"

    def __init__(self, url: str | None = None, timeout_s: float = TIMEOUT_S) -> None:
        self.url = (url or url_from_env()).rstrip("/")
        self.timeout_s = timeout_s

    # --- the protocol ---------------------------------------------------

    def route(
        self,
        waypoints: list[LatLon],
        profile: str = FOOT_PROFILE,
        avoid_polygons: list[dict[str, Any]] | None = None,
        custom_model: CostingModel | None = None,
    ) -> Route:
        paths = self.paths(route_body(waypoints, profile, avoid_polygons, custom_model), waypoints)
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
        """Candidates to score: a detour around a span, or whole-route alternatives.

        `around=(start_m, end_m)` is scope 8.1 step 6's actual request - reroute the part
        that was flagged and keep the rest - and it is built from two pieces that are
        useless apart. The via points either side of the span hold the rest of the route;
        the avoid-area over the span is what makes the answer different at all. Measured
        on a live graph: via points alone return the original line **to the metre**. See
        `core.routing.detour`.

        `around=None` keeps the old behaviour, whole-route alternatives between the same
        endpoints, which is what a caller wants when the whole route is the problem.

        Two facts about GraphHopper that the shape of this method is built around, both
        measured rather than read:

        * `alternative_route.max_paths` is a **total, not an extra**. With `k=3` the first
          path returned is the primary - for the same endpoints, the original route, which
          diverges from itself by 0 m. A caller that already holds the primary (generate
          mode does; it just drew it) asks for one more than it wants and skips the first.
        * **The alternatives algorithm cannot be combined with via points.** Asking for
          both answers `"Currently alternative routes work only with start and end point.
          You tried to use: 4 points"`. A detour is pinned by via points, so a detour
          request is a single-answer request: `k` is honoured for `around=None` and
          ignored for a span. That is not a limitation worth working around by inventing
          candidates the router did not offer - scope 8.1 step 6 iterates over flagged
          *segments*, so the breadth comes from asking about more of them.
        """
        if len(gpx.points) < 2:
            return []

        if around is None:
            waypoints = [
                LatLon(lat=gpx.points[0].lat, lon=gpx.points[0].lon),
                LatLon(lat=gpx.points[-1].lat, lon=gpx.points[-1].lon),
            ]
            areas = list(avoid_polygons or [])
        else:
            start_m, end_m = around
            waypoints = detour_waypoints(gpx, start_m, end_m)
            if len(waypoints) < 2:
                return []
            areas = [*(avoid_polygons or []), detour_area(gpx, start_m, end_m)]

        body = route_body(
            waypoints,
            profile=profile,
            avoid_polygons=areas or None,
            custom_model=custom_model,
            # Only between two points. With via points the server refuses the request
            # outright rather than degrading, and the refusal is a `NoRouteError` that
            # this method would then swallow into an empty list - a detour that silently
            # never happens.
            alternatives=k if len(waypoints) == 2 else 0,
        )
        try:
            paths = self.paths(body, waypoints)
        except (RouterUnavailable, NoRouteError):
            # An alternatives failure degrades scope 8.1 step 6 to flag-but-don't-fix,
            # exactly as `NullRouter` does. It must never take a plan down.
            return []
        return [path_to_route(path, route_id=f"alt-{index}") for index, path in enumerate(paths)]

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]:
        """Snap a track to the graph, returning per-point OSM way ids where the server can.

        Risk R4's answer: `POST /match` returns real `osm_way_id` values per edge on GH 11,
        which is what scope 6.2's accepted-road set needs. A server without map-matching
        enabled returns the track unchanged with no ids, which is what `NullRouter` does —
        an unmatched route is still scoreable from geometry alone.
        """
        import httpx

        from longrun.core.geo.gpx import gpx_string

        # **A GPX body, not JSON.** `/match` is a different endpoint from `/route` with a
        # different content type, and posting JSON to it returns `415 Unsupported Media
        # Type` - which this used to swallow into "no match", so every generated route
        # silently fell back to no way ids at all.
        try:
            response = httpx.post(
                f"{self.url}/match",
                content=gpx_string(track).encode("utf-8"),
                headers={"Content-Type": "application/gpx+xml"},
                params={
                    "profile": FOOT_PROFILE,
                    "details": "osm_way_id",
                    "points_encoded": "false",
                    "ch.disable": "true",
                },
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            paths = response.json().get("paths") or []
        except CacheMiss:
            # Never swallowed. `CacheMiss` is a `LookupError`, so the bare handler below
            # would turn an offline miss into a silent fall-back to geometric snapping -
            # a wrong answer that looks exactly like a right one, in the one place the
            # whole cassette design exists to make loud. Scope 4.4's rule, and M5.4's.
            raise
        except Exception:  # noqa: BLE001 - matching is optional, geometry is not
            return track, [None] * len(track.points)
        if not paths:
            return track, [None] * len(track.points)

        matched = path_to_route(paths[0], route_id=track.id)
        return matched, _way_ids_per_point(paths[0], len(matched.points))

    # --- transport ------------------------------------------------------

    def paths(self, body: dict[str, Any], waypoints: Sequence[LatLon]) -> list[dict[str, Any]]:
        """Post a routing request and return the server's **raw** path dicts.

        Public since M5.4, and raw on purpose: a cached router records exactly what the
        server said and `path_to_route` stays the single decoder, so a cassette holds the
        bytes rather than one reading of them. The same argument `freeze-fixture` makes
        about going through the production store, and M4's about caching a feed payload
        rather than the features parsed out of it.
        """
        import httpx

        try:
            response = httpx.post(f"{self.url}/route", json=body, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise RouterUnavailable(f"no GraphHopper at {self.url}: {exc}") from exc

        if response.status_code >= 400:
            raise _routing_error(response, waypoints)
        paths = response.json().get("paths") or []
        if not paths:
            raise NoRouteError(waypoints[0], waypoints[-1], detail="the router returned no paths")
        return list(paths)


def _routing_error(response: Any, waypoints: Sequence[LatLon]) -> Exception:
    """GraphHopper's error body, turned into the error scope 6.4 asks for.

    A pedestrian graph disconnects at bridges and interchanges far more often than people
    expect, and GraphHopper says so precisely — "Connection between locations not found"
    with the point index. Collapsing that into "routing failed" throws away the only part
    a user could act on.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    messages = [str(hint.get("message") or "") for hint in (payload.get("hints") or [])]
    detail = payload.get("message") or "; ".join(m for m in messages if m) or response.text[:200]
    if response.status_code >= 500:
        return RouterUnavailable(f"GraphHopper returned {response.status_code}: {detail}")
    return NoRouteError(waypoints[0], waypoints[-1], detail=detail)


def _way_ids_per_point(path: dict[str, Any], point_count: int) -> list[int | None]:
    """Expand GraphHopper's `[from, to, value]` detail ranges into one id per point.

    The ranges are over *point indices* and are half-open at the end, which is the detail
    that makes an off-by-one here invisible: a route would score with every way id shifted
    one point along, and every tag-driven scorer would quietly describe the wrong way.
    """
    ids: list[int | None] = [None] * point_count
    for entry in (path.get("details") or {}).get("osm_way_id") or []:
        try:
            start, end, value = int(entry[0]), int(entry[1]), entry[2]
        except (TypeError, ValueError, IndexError):
            continue
        if value is None:
            continue
        for index in range(max(start, 0), min(end, point_count)):
            ids[index] = int(value)
    return ids


__all__ = [
    "CAR_PROFILE",
    "DEFAULT_URL",
    "FOOT_PROFILE",
    "TIMEOUT_S",
    "URL_ENV_VAR",
    "GraphHopperRouter",
    "path_to_route",
    "route_body",
    "url_from_env",
]
