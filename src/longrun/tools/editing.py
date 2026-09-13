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
    def refresh_plan(plan_path: str, date: str, start: str = "07:00") -> dict[str, Any]:
        """Closes `longrun refresh plan.json`, named in `cli/__init__.py` and never built.

        Re-scores the stored *route* rather than re-drawing it: a refresh answers "is this
        still true today", and re-routing would answer a different question.
        """
        from pathlib import Path as _Path

        from longrun.core.models.plan import Plan
        from longrun.core.plan.pipeline import build_plan, score_once
        from longrun.tools.base import scoring_context, start_of

        stored = Plan.model_validate_json(_Path(plan_path).read_text(encoding="utf-8"))
        start_at = start_of(date, start)
        request = stored.request.model_copy(
            update={"date": start_at.date(), "start_time": start_at.time()}
        )
        with scoring_context(settings, stored.route, start_at) as ctx:
            scored = score_once(stored.route, request, ctx, start_at=start_at)
            fresh = build_plan(
                scored,
                request,
                profile=stored.profile,
                coverage=scored.coverage,
                manifest=stored.manifest,
            )
        return {
            "plan_id": fresh.id,
            "was": stored.request.date.isoformat(),
            "now": start_at.date().isoformat(),
            "residual_flags": len(fresh.residual_flags),
            "verify": fresh.verify.summary() if fresh.verify else None,
        }

    @server.tool(name="pin_waypoint", description="Scope 7.8: not available in this build.")
    def pin_waypoint(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "pin_waypoint",
            "a via point is a property of the request, and editing a stored request in "
            "place has no owner yet - the loop takes its waypoints from `PlanRequest`",
            milestone="M7",
        )

    @server.tool(name="place_notes", description="Scope 7.8: not available in this build.")
    def place_notes(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "place_notes",
            "there is no user-note store: scope 7.2 wants notes as a hostility prior and "
            "nothing models one",
            milestone="M6",
        )


__all__ = ["register"]
