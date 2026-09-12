"""The MCP tool layer (scope 4.1, 7, 10.2).

The server is assembled but never run: what is worth testing is the *mapping* - which
scope 7 tools exist, which are honestly absent, and that nothing the scope names is simply
missing from the list. The wrappers themselves are thin by design, and the functions under
them have their own tests.

`mcp` is the `tools` extra, so these skip when it is not installed - the same treatment
`ingest` gets, and the reason CI now syncs the extras.
"""

from __future__ import annotations

from typing import Any

import pytest

from longrun.core.scorers.registry import SCORERS

mcp = pytest.importorskip("mcp.server.mcpserver", reason="the `tools` extra is not installed")


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    from longrun.tools.server import build_server

    server = build_server()
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


#: Scope 7's own names, read off the tool tables. The five at the end are the ones this
#: build cannot answer; they are registered anyway and say why.
ABSENT = {"cue_sheet", "transit_at", "pin_waypoint", "place_notes", "imagery_tile", "render"}


def test_every_scorer_is_reachable_as_a_tool(tools: dict[str, Any]) -> None:
    """Scope 3.9: a capability exists as a tool before it gets a UI control.

    Asserted against `SCORERS` rather than a hand-written list, so a scorer added in a
    later milestone and *not* exposed fails here instead of being quietly CLI-only.
    """
    missing = sorted(name for name in SCORERS if name not in tools)
    assert not missing, f"scorers with no tool: {missing}"


def test_the_tools_that_cannot_answer_are_registered_and_say_why(tools: dict[str, Any]) -> None:
    """M4's `NOT_YET_IMPLEMENTED` treatment, at the tool layer.

    A caller cannot tell a tool that is absent from the list from one that found nothing,
    so a capability the scope names and this build lacks is registered and reports the
    reason and the milestone.
    """
    for name in ABSENT:
        assert name in tools, f"{name} should be registered and honest, not omitted"


@pytest.mark.parametrize("name", sorted(ABSENT))
def test_an_absent_tool_reports_a_reason_and_a_milestone(name: str) -> None:
    from longrun.tools.server import build_server

    server = build_server()
    tool = next(t for t in server._tool_manager.list_tools() if t.name == name)
    answer = tool.fn()

    assert answer["checked"] is False
    assert answer["reason"], "an absent capability explains itself"
    assert answer["milestone"], "and says when it is expected"


def test_the_routing_and_editing_tools_the_scope_names_are_present(tools: dict[str, Any]) -> None:
    """The five that landed here because scope 9 and 10.1 already promised them."""
    for name in (
        "route",
        "alternatives",
        "map_match",
        "elevation_profile",
        "pacing_model",
        "import_route",
        "lock_segment",
        "route_diff",
        "distance_markers",
        "refresh_plan",
        "gpx_read",
        "gpx_write",
        "gpx_verify",
    ):
        assert name in tools


def test_a_tool_answers_rather_than_raising(tmp_path: Any) -> None:
    """Scope 3.6 at the one surface where the caller is a program.

    A program reading a stack trace learns less than one reading a reason, so a bad input
    comes back as `checked: false` rather than as an exception through the transport.
    """
    from longrun.tools.server import build_server

    server = build_server()
    gpx_read = next(t for t in server._tool_manager.list_tools() if t.name == "gpx_read")

    answer = gpx_read.fn(gpx_path=str(tmp_path / "nothing-here.gpx"))

    assert answer["checked"] is False
    assert "reason" in answer


def test_import_route_reads_a_real_gpx(tmp_path: Any) -> None:
    """`import_route` is `gpx_read` under the name scope 7.8 gives it."""
    from longrun.core.geo.gpx import gpx_write
    from longrun.core.models.geometry import Route, RoutePoint
    from longrun.tools.server import build_server

    route = Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.001, lon=-122.41, cum_dist_m=i * 111.0) for i in range(5)
        ],
    )
    path = gpx_write(route, tmp_path / "r.gpx")

    server = build_server()
    tool = next(t for t in server._tool_manager.list_tools() if t.name == "import_route")
    answer = tool.fn(gpx_path=str(path))

    assert answer["points"] == 5
    assert answer["length_m"] > 0


def test_the_server_says_what_it_is_for() -> None:
    """The instructions are the first thing a chat client reads, and they carry the one
    rule a caller most needs: tools measure, they do not decide."""
    from longrun.tools.server import build_server

    server = build_server()
    assert "do not decide" in (server.instructions or "")
