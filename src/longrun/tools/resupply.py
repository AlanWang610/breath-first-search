"""Scope 7.5: water, toilets, food, and the gaps between them.

Each tool is a thin wrapper over a scorer that already exists - the value of this module is
the mapping, not the code. A tool the scope names and this build cannot provide is
registered anyway and reports why, because a caller cannot tell an absent tool from a tool
that found nothing.
"""

from __future__ import annotations

from typing import Any

from longrun.tools.base import ToolSettings, register_scorers

#: Scope 7 tool name -> the scorer that answers it.
TOOLS = {"services_along": "services_along", "resupply_schedule": "resupply_schedule"}


def register(server: Any, settings: ToolSettings) -> None:
    register_scorers(server, settings, TOOLS)


__all__ = ["TOOLS", "register"]
