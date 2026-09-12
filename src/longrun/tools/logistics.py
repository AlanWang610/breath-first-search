"""Scope 7.7: getting there, getting out, and being reachable.

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
    "transit": "transit",
    "bailouts": "bailouts",
    "crew_points": "crew_points",
    "cell_coverage": "cell_coverage",
    "start_time_optimizer": "start_time_optimizer",
}


def register(server: Any, settings: ToolSettings) -> None:
    register_scorers(server, settings, TOOLS)

    @server.tool(
        name="transit_at",
        description="Scope 7: transit_at - not available in this build.",
    )
    def _transit_at(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "transit_at",
            "GTFS is loaded as a per-stop service summary, not as stop_times - which "
            "`core/data/gtfs.py` states and defends: a departure board needs GTFS-RT "
            "to be worth much",
            milestone="unscheduled",
        )


__all__ = ["TOOLS", "register"]
