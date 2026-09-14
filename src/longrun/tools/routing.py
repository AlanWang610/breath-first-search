"""Scope 7.1: drawing a line, and what the ground under it does.

The one group whose tools are not scorers. `pacing_model` is scope 7.3 and the scaffold's
module list gives it no home of its own; it goes here because an ETA is a property of a
drawn route, and a group of one would be a file for the sake of the table.
"""

from __future__ import annotations

from typing import Any

from longrun.tools.base import ToolSettings, read_route, start_of, unavailable


def register(server: Any, settings: ToolSettings) -> None:
    @server.tool(name="geocode", description="Scope 8.1 step 1: a place name to a point.")
    def geocode_tool(query: str, limit: int = 5) -> dict[str, Any]:
        """The last thing standing between a sentence and a `PlanRequest`."""
        from longrun.core.data.geocode import geocode
        from longrun.core.models.geometry import Route, RoutePoint
        from longrun.tools.base import scoring_context, start_of

        # A geocode needs a cache and a budget, which live on a context, and a context is
        # built around a route. A one-point stand-in is honest about that rather than
        # opening a second door for this one tool to use.
        stand_in = Route(
            id="geocode",
            points=[
                RoutePoint(lat=0.0, lon=0.0, cum_dist_m=0.0),
                RoutePoint(lat=0.0, lon=0.001, cum_dist_m=111.0),
            ],
        )
        with scoring_context(settings, stand_in, start_of("2026-01-01")) as ctx:
            found = geocode(query, ctx, limit=limit)
        return {
            "query": found.query,
            "checked": found.answered,
            "reason": found.reason,
            "places": [
                {"name": p.name, "lat": p.lat, "lon": p.lon, "kind": p.kind} for p in found.places
            ],
        }

    @server.tool(name="route", description="Scope 7.1: draw a pedestrian route.")
    def route(points: list[list[float]], profile: str = "foot") -> dict[str, Any]:
        """`points` are [lat, lon], the order everything outside the router uses."""
        from longrun.core.models.geometry import LatLon
        from longrun.core.routing.base import NoRouteError, RouterUnavailable
        from longrun.core.routing.graphhopper import GraphHopperRouter

        waypoints = [LatLon(lat=p[0], lon=p[1]) for p in points]
        try:
            drawn = GraphHopperRouter(settings.router_url).route(waypoints, profile=profile)
        except (RouterUnavailable, NoRouteError) as exc:
            return unavailable("route", f"{type(exc).__name__}: {exc}")
        return {"route_id": drawn.id, "length_m": drawn.length_m, "points": len(drawn.points)}

    @server.tool(name="alternatives", description="Scope 7.1: candidates to score.")
    def alternatives(
        gpx_path: str, start_m: float | None = None, end_m: float | None = None, k: int = 3
    ) -> dict[str, Any]:
        """A detour around a span, or whole-route alternatives when no span is given."""
        from longrun.core.routing.graphhopper import GraphHopperRouter

        around = None if start_m is None or end_m is None else (start_m, end_m)
        found = GraphHopperRouter(settings.router_url).alternatives(
            read_route(gpx_path), around, k=k
        )
        return {
            "count": len(found),
            "candidates": [{"id": r.id, "length_m": r.length_m} for r in found],
        }

    @server.tool(name="map_match", description="Scope 7.1: snap a track to the graph.")
    def map_match(gpx_path: str) -> dict[str, Any]:
        from longrun.core.routing.graphhopper import GraphHopperRouter

        matched, way_ids = GraphHopperRouter(settings.router_url).map_match(read_route(gpx_path))
        known = sum(1 for w in way_ids if w is not None)
        return {"points": len(matched.points), "way_ids_known": known}

    @server.tool(name="elevation_profile", description="Scope 7.1: gain, loss and climbs.")
    def elevation_profile(gpx_path: str, date: str, start: str = "07:00") -> dict[str, Any]:
        from longrun.core.geo.dem import elevation_profile as profile_of
        from longrun.core.geo.dem import sample_elevation
        from longrun.tools.base import scoring_context

        route = read_route(gpx_path)
        with scoring_context(settings, route, start_of(date, start)) as ctx:
            elevations = sample_elevation(route, ctx.rasters)
        return profile_of(route, elevations).model_dump(mode="json")

    @server.tool(name="pacing_model", description="Scope 7.3: ETAs along the route.")
    def pacing_model(gpx_path: str, date: str, start: str = "07:00") -> dict[str, Any]:
        from longrun.core.geo.dem import sample_elevation
        from longrun.core.pacing.model import pacing_model as pace
        from longrun.tools.base import scoring_context

        route = read_route(gpx_path)
        start_at = start_of(date, start)
        with scoring_context(settings, route, start_at) as ctx:
            elevations = sample_elevation(route, ctx.rasters)
        etas = pace(route, start_at, elevations=elevations)
        return {
            "start": etas.etas[0].isoformat() if etas.etas else None,
            "finish": etas.etas[-1].isoformat() if etas.etas else None,
            "moving_time_s": etas.moving_time_s,
            # Scope 6.2's obligations travel with the ETAs, never separately: a caller that
            # gets times without the extrapolation warning has the least useful half.
            "caveats": list(etas.caveats),
        }


__all__ = ["register"]
