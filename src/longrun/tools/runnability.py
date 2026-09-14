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
        description="Scope 7: cue_sheet - not available in this build.",
    )
    def _cue_sheet(**kwargs: Any) -> dict[str, Any]:
        # `work`, and more of it than the reason makes it sound. Flipping `instructions` to
        # True in `route_body` is the obvious first move and it would fail silently:
        # `instructions` is not among the fields `CachedRouter._paths` keys a route on, so
        # every cassette recorded without instructions would be served to a request that
        # wants them, and the result would be a cue sheet with no turns in it - which is
        # indistinguishable from a route that has none. The key has to learn the flag first.
        return unavailable(
            "cue_sheet",
            "GraphHopper turn instructions are not requested - `route_body` sets "
            "`instructions: False` - so there is nothing to build a cue sheet from",
            blocked_on="work",
        )


__all__ = ["TOOLS", "register"]
