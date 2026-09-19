"""Scope 7.2: is this runnable? Tag-only measurements over the corridor.

Each tool is a thin wrapper over a scorer that already exists - the value of this module is
the mapping, not the code. A tool the scope names and this build cannot provide is
registered anyway and reports why, because a caller cannot tell an absent tool from a tool
that found nothing.
"""

from __future__ import annotations

from typing import Any

from longrun.tools.base import ToolSettings, register_scorers, unavailable

#: Scope 7 tool name -> the scorer that answers it.
TOOLS = {
    "segment_hostility": "segment_hostility",
    "crossings": "crossings",
    "stop_density": "stop_density",
    "surface_profile": "surface_profile",
}


def register(server: Any, settings: ToolSettings) -> None:
    register_scorers(server, settings, TOOLS)

    @server.tool(
        name="cue_sheet",
        description=(
            "Scope 7.2: the turn list for a route drawn between these points, with turn "
            "count and ambiguity flags."
        ),
    )
    def cue_sheet(points: list[list[float]], profile: str = "foot") -> dict[str, Any]:
        """`points` are [lat, lon], the order everything outside the router uses.

        **Scope 7.2 spells this `cue_sheet(gpx)` and this takes points instead.** A GPX track
        has no turns in it, and there are exactly two ways to give one turns. Re-routing
        between its endpoints draws a *different line* and describes that - the dishonesty
        this tool was blocked on, wearing a different hat. Map-matching it does work:
        measured on 2026-09-18 against GraphHopper 11, `POST /match?instructions=true`
        returns instructions, which would also make the frame shift structurally zero. That
        is the follow-up, and it needs `CachedRouter.map_match`'s key to learn the flag by
        elision exactly as `route_args` now does.
        """
        from longrun.core.models.geometry import LatLon
        from longrun.core.routing.base import NoRouteError, RouterUnavailable
        from longrun.core.routing.graphhopper import GraphHopperRouter

        waypoints = [LatLon(lat=float(p[0]), lon=float(p[1])) for p in points]
        try:
            route, sheet = GraphHopperRouter(settings.router_url).route_cues(
                waypoints, profile=profile
            )
        except (RouterUnavailable, NoRouteError) as exc:
            # No `blocked_on`: the tool exists now, so this is a fact about the call rather
            # than a gap in the build.
            return unavailable("cue_sheet", f"{type(exc).__name__}: {exc}")

        return {
            "name": "cue_sheet",
            "checked": sheet.checked,
            "reason": sheet.reason,
            "route_id": route.id,
            "length_m": round(route.length_m, 1),
            "turn_count": sheet.turn_count,
            "ambiguous_count": len(sheet.ambiguous),
            "cues": [c.model_dump(mode="json") for c in sheet.cues],
        }


__all__ = ["TOOLS", "register"]
