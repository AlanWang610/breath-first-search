"""`longrun repair` — score a user-supplied GPX and emit a plan sheet (scope 6.1, 10.1).

Repair mode is the entry path scope 6.1 expects experienced users to take, and scope 8.1
step 3 lets it skip straight to scoring: the user supplies the geometry, so no router is
required. That makes this the first end-to-end command, and per scope 10.1 it is also the
golden-test harness — every golden route runs through here with no model in the loop.

Scorers are looked up by name and any that is not yet implemented is reported as
`unavailable` rather than quietly skipped. That is the same rule scope 3.6 applies to
external data, turned on our own build state: a plan sheet must not imply a scorer ran
clean when it does not exist.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any

import typer

from longrun.core.data.cache import SqliteCache, cache_path_from_env, offline_from_env
from longrun.core.data.file_store import FileLayerStore, FileRasterStore, LayerNotFound
from longrun.core.export.sheet_md import render_markdown
from longrun.core.geo.dem import elevation_profile, sample_elevation
from longrun.core.geo.gpx import GpxError, gpx_read
from longrun.core.geo.matching import assign_way_ids
from longrun.core.geo.segments import corridor, segment_route
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageEntry, CoverageManifest
from longrun.core.models.measurement import ScorerResult
from longrun.core.models.plan import Plan
from longrun.core.models.request import PlanRequest
from longrun.core.pacing.model import pacing_model
from longrun.core.plan.arbitrate import residual_flags
from longrun.core.preferences.store import load_profile
from longrun.core.scorers.base import unavailable
from longrun.core.verify.runner import gpx_verify

#: Scorer name -> module path. Scope 8.1 step 5's list, minus the ones whose milestone
#: has not landed; each missing module is reported, never silently omitted.
SCORERS: dict[str, str] = {
    "legality": "longrun.core.scorers.legality",
    "segment_hostility": "longrun.core.scorers.hostility",
    "crossings": "longrun.core.scorers.crossings",
    "stop_density": "longrun.core.scorers.stop_density",
    "surface_profile": "longrun.core.scorers.surface",
    "services_along": "longrun.core.scorers.services",
}

#: Scorers the scope calls for whose milestone has not arrived. Named explicitly so the
#: coverage manifest can say they were not run, instead of the sheet staying silent.
NOT_YET_IMPLEMENTED: dict[str, str] = {
    "sun_exposure": "DSM ray-cast lands in M2",
    "heat_stress": "needs the forecast adapter (M2)",
    "microclimate": "needs the forecast adapter (M2)",
    "air_quality": "needs the AirNow adapter (M2)",
    "lighting": "needs solar geometry wiring (M2)",
    "resupply_schedule": "needs opening-hours parsing (M2)",
    "closures": "needs the jurisdiction adapter registry (M4)",
    "trail_status": "needs the jurisdiction adapter registry (M4)",
    "access_hours": "needs the jurisdiction adapter registry (M4)",
    "hazards": "needs NHD and NWS alerts (M3)",
    "bailouts": "needs GTFS (M3)",
}


def _load_scorer(module_path: str) -> Any | None:
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return None
    return getattr(module, "score", None)


def _run_scorers(
    route: Any, segments: list[Any], ctx: ScorerContext, etas: list[datetime]
) -> list[ScorerResult]:
    """Run every scorer that exists; report every one that does not."""
    results: list[ScorerResult] = []

    for name, module_path in SCORERS.items():
        func = _load_scorer(module_path)
        if func is None:
            results.append(unavailable(name, "scorer not implemented yet"))
            continue
        try:
            results.append(func(route, segments, ctx, etas))
        except Exception as exc:  # a scorer must never take the whole plan down
            results.append(unavailable(name, f"scorer failed: {type(exc).__name__}: {exc}"))

    for name, reason in NOT_YET_IMPLEMENTED.items():
        results.append(unavailable(name, reason))

    for result in results:
        for entry in result.coverage:
            ctx.coverage.record(entry)
    return results


def repair(
    gpx_path: Path = typer.Argument(..., help="Route to score (GPX 1.1)."),
    date: datetime = typer.Option(..., "--date", formats=["%Y-%m-%d"], help="Run date."),
    start: str = typer.Option("07:00", "--start", help="Start time, HH:MM."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Preference profile YAML."),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    out: Path | None = typer.Option(None, "--out", help="Directory for outputs."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    target_km: float | None = typer.Option(None, "--target-km", help="Target distance."),
) -> None:
    """Score an existing route and write a plan sheet."""
    try:
        route = gpx_read(gpx_path, route_id=gpx_path.stem)
    except GpxError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        hour, _, minute = start.partition(":")
        start_at = datetime.combine(date.date(), time_type(int(hour), int(minute or 0)))
    except ValueError as exc:
        typer.echo(f"error: could not read --start {start!r}; expected HH:MM", err=True)
        raise typer.Exit(code=2) from exc

    request = PlanRequest(
        mode="repair",
        date=date.date(),
        start_time=start_at.time(),
        target_distance_km=target_km,
    )
    profile = load_profile(profile_path)
    root = fixtures or Path("data")
    # Either door turns no-miss mode on: the `--offline` flag, or the environment variable
    # that golden and contract runs export. Honouring only the flag would silently ignore a
    # caller who went to the trouble of exporting it.
    offline = offline or offline_from_env()

    # Held open with `with`: the connection is otherwise left to the garbage collector, and
    # on Windows an unclosed SQLite handle keeps a file lock, so a leaked one can stop the
    # next run - or a test's tmp_path cleanup - from removing the file.
    with SqliteCache(cache_path_from_env(), offline=offline) as cache:
        ctx = ScorerContext(
            layers=FileLayerStore(root),
            rasters=FileRasterStore(root),
            cache=cache,
            clock=FrozenClock(start_at),
            coverage=CoverageManifest(),
            profile=profile,
            budget=Budget(),
        )

        # Repair mode has no router, so nothing has told us which way each point lies on.
        # Snap geometrically instead, or every way-tag scorer reads `unknown` for the whole
        # route and the commonest entry mode becomes the least useful one.
        match = _match_ways(route, ctx)
        segments = segment_route(route, way_ids=match.way_ids if match else None)
        if match is not None and not match.is_usable:
            ctx.coverage.record(
                CoverageEntry(
                    source="way_matching",
                    kind="osm_tags",
                    checked=False,
                    reason=(
                        f"only {match.match_rate:.0%} of route points snapped to a way within "
                        f"{match.tolerance_m:.0f} m; tag-driven scorers cover a minority of "
                        f"this route"
                    ),
                )
            )

        # sample_elevation already returns all-None where the DEM has no coverage, which is
        # what "unknown elevation" looks like downstream (scope 12).
        elevations = sample_elevation(route, ctx.rasters)
        elevation = elevation_profile(route, elevations)

        eta_vector = pacing_model(route, start_at, elevations=elevations)
        results = _run_scorers(route, segments, ctx, eta_vector.etas)

        plan = Plan(
            id=f"{route.id}-{uuid.uuid4().hex[:8]}",
            request=request,
            route=route,
            segments=segments,
            results=results,
            etas=eta_vector.etas,
            residual_flags=residual_flags(results, segments, route.length_m),
            coverage=ctx.coverage,
            profile=profile,
        )
        report = gpx_verify(
            route,
            request,
            segments=segments,
            results=results,
            etas=eta_vector.etas,
            elevations=elevations,
        )
        sheet = render_markdown(
            plan, elevation=elevation, verify=report, pacing_caveats=eta_vector.caveats
        )

        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / "sheet.md").write_text(sheet, encoding="utf-8")
            (out / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            typer.echo(f"wrote {out / 'sheet.md'} and {out / 'plan.json'}")
        else:
            _echo_utf8(sheet)


def _match_ways(route: Any, ctx: ScorerContext) -> Any:
    """Snap the route to OSM ways, or None when there is no ways layer to snap to."""
    try:
        ways = ctx.layers.ways_in_corridor(corridor(route))
    except (LayerNotFound, FileNotFoundError):
        ctx.coverage.record(
            CoverageEntry(
                source="way_matching",
                kind="osm_tags",
                checked=False,
                reason="no ways layer available to match the route against",
            )
        )
        return None
    return assign_way_ids(route, ways)


def _echo_utf8(text: str) -> None:
    """Print without tripping over a Windows console's default code page.

    The sheet contains en dashes and degree signs; a cp1252 stdout raises on those, which
    would turn a successful plan into a crash at the last step.
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass
    typer.echo(text)


def register(app: typer.Typer) -> None:
    app.command("repair")(repair)


__all__ = ["register", "repair"]
