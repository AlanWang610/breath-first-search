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


def test_the_agents_door_has_no_way_to_record_a_label(tools: dict[str, Any]) -> None:
    """ADR 0048: every other capability lands here first, and this one never does.

    `tools/` is the surface the planner acts through on a person's behalf, and saying what a
    person thinks of a route is the one thing it must not do on their behalf (ADR 0022). So
    two checks, because either alone has a way round it: no tool is *named* like one, and no
    module under `tools/` *imports* the label core under any name.
    """
    import ast
    from pathlib import Path

    import longrun.tools

    named = sorted(
        name
        for name in tools
        if any(word in name for word in ("label", "verdict", "judge", "prefer", "ledger"))
    )
    assert not named, f"tools that look like a way to record a label: {named}"

    importers = []
    for path in Path(longrun.tools.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(module.startswith("longrun.core.labels") for module in modules):
                importers.append(path.name)
    assert not importers, f"tools/ modules importing the label core: {sorted(importers)}"


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


# --- route, drawn with the runner's vector (M24) ---------------------------------------


def test_the_route_tool_draws_with_the_runners_saved_vector(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chat routed with no model at all until M24, so a saved fit would never have reached
    that surface - nor had `--avoid-high-stress` (scope 3.9: one surface, one vector)."""
    from longrun.core.models.geometry import Route, RoutePoint
    from longrun.core.models.profile import (
        FittedRouting,
        PreferenceEntry,
        PreferenceProfile,
        Provenance,
    )
    from longrun.core.preferences.store import save_profile
    from longrun.core.routing.custom_model import CustomModelParams, to_custom_model
    from longrun.tools.base import ToolSettings
    from longrun.tools.server import build_server

    fitted = CustomModelParams(unpaved=2.0)
    profile = PreferenceProfile(
        routing=PreferenceEntry(
            value=FittedRouting(params=fitted, labeler="runner-1"),
            provenance=Provenance.INFERRED,
        )
    )
    path = save_profile(profile, tmp_path / "profile.yaml")
    monkeypatch.setenv("LONGRUN_PROFILE_PATH", str(path))
    sent: list[Any] = []

    class _Router:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def route(self, waypoints: list[Any], **kwargs: Any) -> Route:
            sent.append(kwargs.get("custom_model"))
            points = [
                RoutePoint(lat=w.lat, lon=w.lon, cum_dist_m=index * 100.0)
                for index, w in enumerate(waypoints)
            ]
            return Route(id="r", points=points)

    monkeypatch.setattr("longrun.core.routing.graphhopper.GraphHopperRouter", _Router)
    server = build_server(ToolSettings(router_url="http://router.invalid"))
    tool = next(t for t in server._tool_manager.list_tools() if t.name == "route")
    ends = [[37.7955, -122.3937], [37.7715, -122.4686]]

    fitted_answer = tool.fn(points=ends)
    neutral_answer = tool.fn(points=ends, neutral=True)

    assert sent == [to_custom_model(fitted, profile), None]
    assert fitted_answer["drawn_with"] == "fitted for runner-1: unpaved=2"
    assert "was not used" in neutral_answer["drawn_with"]
