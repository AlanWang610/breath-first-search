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

    @server.tool(
        name="imagery_tile",
        description=(
            "Scope 7.9: the aerial tile containing a point, for a spot-check of a flagged "
            "segment. US coverage, zoom 16 at most (about 1.9 m a pixel)."
        ),
    )
    def imagery_tile(lat: float, lon: float, zoom: int = 16) -> dict[str, Any]:
        """An aerial tile, base64-encoded, from the configured provider (ADR 0023).

        Scope 7.9 caps this at "~10 calls per plan", which is `Budget.imagery_tiles_max`.
        A tool call opens its own budget, like every tool here, so the cap binds inside a
        plan - where scope 8.1 step 8 would spend it - rather than across separate calls.
        """
        import base64

        from longrun.core.data.cache import SqliteCache
        from longrun.core.data.tiles import TileConfigError, fetch_tile, provider_from_env
        from longrun.core.models.context import Budget

        try:
            provider = provider_from_env()
        except TileConfigError as exc:
            return unavailable("imagery_tile", str(exc), blocked_on="decision")
        if provider is None:
            return unavailable(
                "imagery_tile",
                "tiles are switched off (LONGRUN_TILE_PROVIDER=none)",
                blocked_on="decision",
            )
        if provider.kind != "imagery":
            # Refused rather than served: a spot-check asks what is on the ground, and a
            # topographic map answers a different question while looking like an answer.
            return unavailable(
                "imagery_tile",
                f"the configured provider {provider.id!r} draws a map, not aerial imagery",
                blocked_on="decision",
            )

        with SqliteCache(settings.cache_path or ":memory:", offline=settings.offline) as cache:
            try:
                tile = fetch_tile(provider, lat, lon, zoom, cache=cache, budget=Budget())
            except Exception as exc:  # noqa: BLE001 - a tile that will not come is a reason
                return unavailable("imagery_tile", f"{type(exc).__name__}: {exc}")

        answer: dict[str, Any] = {
            "name": "imagery_tile",
            "provider": tile.provider,
            "z": tile.z,
            "x": tile.x,
            "y": tile.y,
            "requested_zoom": tile.requested_zoom,
            "url": tile.url,
            "attribution": tile.attribution,
        }
        if tile.requested_zoom != tile.z:
            answer["note"] = (
                f"zoom {tile.requested_zoom} clamped to {tile.z}, the most {tile.provider} serves"
            )
        if not tile.found:
            # The tool exists and found nothing *here*, so no `blocked_on`: that is a fact
            # about this point, not a gap in the build.
            return {**answer, "checked": False, "reason": tile.reason}
        return {
            **answer,
            "checked": True,
            "media_type": tile.media_type,
            "data_base64": base64.b64encode(tile.data or b"").decode("ascii"),
        }

    @server.tool(name="render", description="Scope 7.9: not available in this build.")
    def render(**kwargs: Any) -> dict[str, Any]:
        return unavailable(
            "render",
            "a tile provider is configured (ADR 0023); what is missing is compositing its "
            "tiles and the route into one image, which needs an image library this build "
            "does not depend on",
            blocked_on="work",
        )


def _request(start_at: Any) -> Any:
    from longrun.core.models.request import PlanRequest

    return PlanRequest(mode="repair", date=start_at.date(), start_time=start_at.time())


__all__ = ["register"]
