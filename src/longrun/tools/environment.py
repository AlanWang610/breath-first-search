"""Scope 7.4: sun, heat, weather and light at projected arrival time.

Each tool is a thin wrapper over a scorer that already exists - the value of this module is
the mapping, not the code. A tool the scope names and this build cannot provide is
registered anyway and reports why, because a caller cannot tell an absent tool from a tool
that found nothing.
"""

from __future__ import annotations

from typing import Any

from longrun.tools.base import ToolSettings, register_scorers

#: Scope 7 tool name -> the scorer that answers it.
TOOLS = {
    "microclimate": "microclimate",
    "sun_exposure": "sun_exposure",
    "heat_stress": "heat_stress",
    "lighting": "lighting",
    "air_quality": "air_quality",
}


def register(server: Any, settings: ToolSettings) -> None:
    register_scorers(server, settings, TOOLS)


__all__ = ["TOOLS", "register"]
