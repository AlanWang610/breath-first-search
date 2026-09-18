"""Scope 7.8: changing a plan rather than measuring one.

Five of these are new here and cheap, because scope 9 and 10.1 already promise them and
the pieces each needs already exist. Two are not, and say so.
"""

from __future__ import annotations

from typing import Any

from longrun.tools.base import ToolSettings, read_route, unavailable


def register(server: Any, settings: ToolSettings) -> None:
    @server.tool(name="import_route", description="Scope 7.8: read a GPX into a route.")
    def import_route(gpx_path: str) -> dict[str, Any]:
        route = read_route(gpx_path)
        return {
            "route_id": route.id,
            "length_m": route.length_m,
            "points": len(route.points),
            "name": route.name,
        }

    @server.tool(name="lock_segment", description="Scope 7.8: exclude a range from rerouting.")
    def lock_segment(
        scratchpad_path: str, start_m: float, end_m: float, reason: str | None = None
    ) -> dict[str, Any]:
        """Writes through the scratchpad, which is where a lock has to live.

        A lock held only in a caller's memory is one the loop cannot honour on the next
        round and a resume cannot know about - and scope 6.4 makes locks a property of the
        plan, not of the conversation.
        """
        from longrun.core.plan.scratchpad import Scratchpad

        pad = Scratchpad.load(scratchpad_path)
        pad.lock(start_m, end_m, reason=reason)
        pad.save(scratchpad_path)
        return {"locked": [r.model_dump(mode="json") for r in pad.locked]}

    @server.tool(name="route_diff", description="Scope 7.8: where two routes differ.")
    def route_diff_tool(gpx_a: str, gpx_b: str) -> dict[str, Any]:
        from longrun.core.plan.diff import route_diff

        diff = route_diff(read_route(gpx_a), read_route(gpx_b))
        return {
            "length_delta_m": diff.length_delta_m,
            "shared_fraction": round(diff.shared_fraction, 3),
            "summary": diff.summary(),
            "spans": [
                {
                    "start_m": round(s.start_m, 1),
                    "end_m": round(s.end_m, 1),
                    "max_offset_m": round(s.max_offset_m, 1),
                }
                for s in diff.spans
            ],
        }

    @server.tool(name="distance_markers", description="Scope 7.8: waypoints every N metres.")
    def distance_markers(gpx_path: str, interval_m: float = 1000.0) -> dict[str, Any]:
        route = read_route(gpx_path)
        markers: list[dict[str, Any]] = []
        target = interval_m
        for point in route.points:
            if point.cum_dist_m >= target:
                markers.append({"at_m": round(target), "lat": point.lat, "lon": point.lon})
                target += interval_m
        return {"interval_m": interval_m, "markers": markers}

    @server.tool(name="refresh_plan", description="Scope 7.8: re-score a stored plan.")
    def refresh_plan(
        plan_path: str, date: str, start: str = "07:00", write: bool = False
    ) -> dict[str, Any]:
        """The same function `longrun refresh` calls, so the two cannot drift.

        Re-scores the stored *route* rather than re-drawing it: a refresh answers "is this
        still true today", and re-routing would answer a different question.

        `write` defaults to False here and to True on the CLI, deliberately. An agent
        calling a tool that reads like a query should not silently overwrite the user's file.

        The context is opened with the stored plan's own snapshot pins. `ToolSettings.snapshot`
        defaults to empty and `from_env` never fills it, so every fresh coverage entry
        reported `vintage=None` while the manifest it was merged into still claimed the
        original pins.
        """
        from dataclasses import replace as _replace
        from pathlib import Path as _Path

        from longrun.core.models.plan import Plan
        from longrun.core.plan.refresh import rescore_plan
        from longrun.tools.base import scoring_context, start_of

        stored = Plan.model_validate_json(_Path(plan_path).read_text(encoding="utf-8"))
        start_at = start_of(date, start)
        pinned = _replace(settings, snapshot=stored.manifest.snapshot)
        with scoring_context(pinned, stored.route, start_at) as ctx:
            out = rescore_plan(stored, ctx, start_at=start_at)
        if write:
            _Path(plan_path).write_text(out.plan.model_dump_json(indent=2), encoding="utf-8")
        return {
            "plan_id": out.plan.id,
            "was": stored.request.date.isoformat(),
            "now": start_at.date().isoformat(),
            "rescored": list(out.delta.rescored),
            "carried": list(out.delta.carried),
            "changed": out.delta.lines(),
            "residual_flags": len(out.plan.residual_flags),
            "verify": out.plan.verify.summary() if out.plan.verify else None,
            "written": plan_path if write else None,
        }

    @server.tool(name="pin_waypoint", description="Scope 7.8: not available in this build.")
    def pin_waypoint(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "pin_waypoint",
            "a via point is a property of the request, and editing a stored request in "
            "place has no owner yet - the loop takes its waypoints from `PlanRequest`",
            blocked_on="work",
        )

    @server.tool(name="place_notes", description="Scope 7.8: not available in this build.")
    def place_notes(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "place_notes",
            "there is no user-note store: scope 7.2 wants notes as a hostility prior and "
            "nothing models one",
            blocked_on="work",
        )


__all__ = ["register"]
