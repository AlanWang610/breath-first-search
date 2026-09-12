"""Scope 7.9: reading, writing and checking a route.

`gpx_verify` is the one that matters: scope 8.1 step 9 sends its failures back to step 6,
and its three-state result - passed, failed, **skipped** - is the thing a caller must not
flatten. A check that could not run is not a check that succeeded.
"""

from __future__ import annotations

from typing import Any

from longrun.tools.base import ToolSettings, read_route, start_of, unavailable


def register(server: Any, settings: ToolSettings) -> None:
    @server.tool(name="gpx_read", description="Scope 7.9: parse a GPX 1.1 file.")
    def gpx_read_tool(gpx_path: str) -> dict[str, Any]:
        from longrun.core.geo.gpx import GpxError

        try:
            route = read_route(gpx_path)
        except GpxError as exc:
            return unavailable("gpx_read", str(exc))
        return {"route_id": route.id, "points": len(route.points), "length_m": route.length_m}

    @server.tool(name="gpx_write", description="Scope 7.9: write a route back out as GPX.")
    def gpx_write_tool(gpx_path: str, destination: str) -> dict[str, Any]:
        from longrun.core.geo.gpx import gpx_write

        written = gpx_write(read_route(gpx_path), destination)
        return {"wrote": str(written)}

    @server.tool(name="gpx_verify", description="Scope 7.9: the ten checks.")
    def gpx_verify_tool(gpx_path: str, date: str, start: str = "07:00") -> dict[str, Any]:
        from longrun.core.plan.pipeline import score_once
        from longrun.tools.base import scoring_context

        route = read_route(gpx_path)
        start_at = start_of(date, start)
        request = _request(start_at)
        with scoring_context(settings, route, start_at) as ctx:
            scored = score_once(route, request, ctx, start_at=start_at)
        report = scored.verify
        if report is None:  # pragma: no cover - a pass always verifies
            return unavailable("gpx_verify", "the pass produced no verification report")
        return {
            "summary": report.summary(),
            "passed": report.passed,
            # Every check, in all three states. A caller that sees only failures cannot
            # tell a check that ran clean from one that never ran - scope 12's subject.
            "checks": [
                {"number": c.number, "name": c.name, "status": c.status, "offenders": c.offenders}
                for c in report.results
            ],
        }

    @server.tool(name="imagery_tile", description="Scope 7.9: not available in this build.")
    def imagery_tile(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "imagery_tile",
            "no imagery provider is configured: `Budget.spend_imagery_tile` meters a cost "
            "nothing incurs, and a provider carries a scope 14 obligation",
            milestone="M7",
        )

    @server.tool(name="render", description="Scope 7.9: not available in this build.")
    def render(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "render",
            "a static map needs a raster tile provider with a scope 14 obligation; the "
            "HTML sheet draws route, flags and elevation on a blank ground instead",
            milestone="M7",
        )


def _request(start_at: Any) -> Any:
    from longrun.core.models.request import PlanRequest

    return PlanRequest(mode="repair", date=start_at.date(), start_time=start_at.time())


__all__ = ["register"]
