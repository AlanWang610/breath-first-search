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


#: Scope 7's own names, read off the tool tables: the five this build cannot answer. They
#: are registered anyway and say why. `imagery_tile` left this set with ADR 0023.
#: Tools registered and honest about not working. M10 removed `cue_sheet` from this set
#: by building it and M11 removed `pin_waypoint` by giving a stored request an owner to be
#: edited through; `render` follows when the SVG renderer lands.
ABSENT = {"transit_at", "place_notes", "render"}


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
    reason and what blocks it.
    """
    for name in ABSENT:
        assert name in tools, f"{name} should be registered and honest, not omitted"


@pytest.mark.parametrize("name", sorted(ABSENT))
def test_an_absent_tool_reports_a_reason_and_what_blocks_it(name: str) -> None:
    """This test used to require a `milestone`, and so it enforced the defect.

    It asserted every absent tool "says when it is expected" - and by the end of M7 four of
    the six were naming a milestone that had already shipped without them, to every MCP
    client that asked. A date in code is a claim with an expiry nobody tracks. What blocks
    a tool does not expire, so that is what is required now, from a closed vocabulary.
    """
    from typing import get_args

    from longrun.tools.base import Blocker
    from longrun.tools.server import build_server

    server = build_server()
    tool = next(t for t in server._tool_manager.list_tools() if t.name == name)
    answer = tool.fn()

    assert answer["checked"] is False
    assert answer["reason"], "an absent capability explains itself"
    assert answer.get("blocked_on") in get_args(Blocker), "and says what stands in the way"
    assert "milestone" not in answer, "a promise of when goes stale once its date passes"


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


# --- imagery_tile, which left ABSENT with ADR 0023 ---------------------------------


def _imagery_tool(settings: Any = None) -> Any:
    from longrun.tools.server import build_server

    server = build_server(settings)
    return next(t for t in server._tool_manager.list_tools() if t.name == "imagery_tile")


def test_imagery_tile_returns_a_tile_and_says_when_it_clamped(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the tool, with the server faked. Asked for zoom 18, which USGS
    does not serve, it returns the zoom-16 tile and says so rather than a 404 dressed up as
    "no imagery here"."""
    import base64

    import httpx

    from longrun.tools.base import ToolSettings

    class _Ok:
        status_code = 200
        content = b"\xff\xd8 imagery"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: _Ok())
    tool = _imagery_tool(ToolSettings(cache_path=tmp_path / "c.sqlite"))

    answer = tool.fn(lat=37.7715, lon=-122.4686, zoom=18)

    assert answer["checked"] is True
    assert (answer["z"], answer["requested_zoom"]) == (16, 18)
    assert "clamped" in answer["note"]
    assert base64.b64decode(answer["data_base64"]) == b"\xff\xd8 imagery"
    assert "The National Map" in answer["attribution"]


def test_imagery_tile_switched_off_says_it_is_a_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LONGRUN_TILE_PROVIDER", "none")

    answer = _imagery_tool().fn(lat=37.77, lon=-122.47)

    assert answer["checked"] is False
    assert answer["blocked_on"] == "decision"


def test_imagery_tile_refuses_a_map_provider_rather_than_serving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spot-check asks what is on the ground. A topographic map answers a different
    question while looking like an answer, so it is refused, not returned."""
    monkeypatch.setenv("LONGRUN_TILE_PROVIDER", "usgs-topo")

    answer = _imagery_tool().fn(lat=37.77, lon=-122.47)

    assert answer["checked"] is False
    assert "not aerial imagery" in answer["reason"]


def test_the_render_tool_is_now_blocked_on_work_not_on_a_decision() -> None:
    """The decision `render` waited on was made in ADR 0023. Leaving it labelled `decision`
    would be the stale-label defect M7.4 fixed, recreated one commit later."""
    from longrun.tools.server import build_server

    server = build_server()
    render = next(t for t in server._tool_manager.list_tools() if t.name == "render")

    assert render.fn()["blocked_on"] == "work"
