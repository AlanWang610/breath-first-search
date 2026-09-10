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
import inspect
import sys
import time
import uuid
from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any, cast

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
from longrun.core.models.plan import Manifest, Plan, SnapshotPins, ToolCall
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
    "microclimate": "longrun.core.scorers.microclimate",
    # Order is dependency order: `sun_exposure` before `heat_stress`, which reads the
    # sunlit fraction back out of it (scope 7.4: "from sun exposure + temp + ...").
    "sun_exposure": "longrun.core.scorers.sun",
    "heat_stress": "longrun.core.scorers.heat",
    "lighting": "longrun.core.scorers.lighting",
    "air_quality": "longrun.core.scorers.air_quality",
    # After heat_stress: scope 8.3 scales the dry-gap thresholds down with WBGT.
    "resupply_schedule": "longrun.core.scorers.resupply_schedule",
}

#: Scorers the scope calls for whose milestone has not arrived. Named explicitly so the
#: coverage manifest can say they were not run, instead of the sheet staying silent.
NOT_YET_IMPLEMENTED: dict[str, str] = {
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


def _call(
    func: Any,
    route: Any,
    segments: list[Any],
    ctx: ScorerContext,
    etas: list[datetime],
    prior: list[ScorerResult],
) -> ScorerResult:
    """Invoke a scorer, handing it earlier results only if it asks for them.

    Two scope 7.4/7.5 tools are defined in terms of another scorer's output rather than of
    raw data: `heat_stress` is "WBGT ... from **sun exposure** + temp + humidity + wind",
    and scope 8.3's dry-gap thresholds "both scale down with WBGT". So the dependency is
    the scope's, not an implementation shortcut.

    Inspected rather than passed to everything, so the six scorers that are pure functions
    of `(route, segments, ctx, etas)` stay that way and cannot quietly grow a dependency on
    execution order. `SCORERS` is an ordered dict, and that order is the dependency order.
    """
    if "prior" in inspect.signature(func).parameters:
        return cast("ScorerResult", func(route, segments, ctx, etas, prior=prior))
    return cast("ScorerResult", func(route, segments, ctx, etas))


def _run_scorers(
    route: Any,
    segments: list[Any],
    ctx: ScorerContext,
    etas: list[datetime],
    manifest: Manifest | None = None,
) -> list[ScorerResult]:
    """Run every scorer that exists; report every one that does not.

    Each is timed into the manifest. Scope 6.4 wants per-tool elapsed time recorded so the
    ~3-minute budget is measured rather than assumed, and until now `Manifest.tool_calls`
    existed with nothing writing to it - so "which scorer is slow" was a question only a
    profiler could answer, and only on a machine that had one.
    """
    results: list[ScorerResult] = []

    for name, module_path in SCORERS.items():
        func = _load_scorer(module_path)
        if func is None:
            results.append(unavailable(name, "scorer not implemented yet"))
            continue
        started = time.perf_counter()
        try:
            results.append(_call(func, route, segments, ctx, etas, results))
        except Exception as exc:  # a scorer must never take the whole plan down
            results.append(unavailable(name, f"scorer failed: {type(exc).__name__}: {exc}"))
        if manifest is not None:
            manifest.record(ToolCall(tool=name, elapsed_s=time.perf_counter() - started))

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
    snapshot_path: Path | None = typer.Option(
        None, "--snapshot", help="Data-snapshot pins (JSON), recorded in the manifest."
    ),
    out: Path | None = typer.Option(None, "--out", help="Directory for outputs."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    cache_path: Path | None = typer.Option(
        None, "--cache", help="Cache/cassette file. Overrides LONGRUN_CACHE_DIR."
    ),
    remote_rasters: bool = typer.Option(
        False, "--remote-rasters", help="Read 3DEP and canopy over the network."
    ),
    target_km: float | None = typer.Option(None, "--target-km", help="Target distance."),
    utc_offset: float | None = typer.Option(
        None, "--utc-offset", help="Hours from UTC at the route, e.g. -7 for PDT."
    ),
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
        utc_offset_hours=utc_offset,
    )
    profile = load_profile(profile_path)
    root = fixtures or Path("data")
    # Either door turns no-miss mode on: the `--offline` flag, or the environment variable
    # that golden and contract runs export. Honouring only the flag would silently ignore a
    # caller who went to the trouble of exporting it.
    offline = offline or offline_from_env()

    try:
        snapshot = _load_snapshot(snapshot_path)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: could not read --snapshot {snapshot_path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Held open with `with`: the connection is otherwise left to the garbage collector, and
    # on Windows an unclosed SQLite handle keeps a file lock, so a leaked one can stop the
    # next run - or a test's tmp_path cleanup - from removing the file.
    # Precedence: --cache, then LONGRUN_CACHE_DIR, then in-memory. A golden route pins
    # its cassette in the route directory and passes it here, because the autouse
    # fixture that clears LONGRUN_* would otherwise leave the env var with nothing in
    # it and every forecast key would miss.
    store = cache_path or cache_path_from_env()
    with SqliteCache(store, offline=offline) as cache:
        layers = FileLayerStore(root)
        # Scope 6.4: every source in the manifest carries a vintage. The store is what the
        # scorers ask, so the pins go in here rather than being stitched on afterwards -
        # a coverage entry then reports the vintage of the data it actually read.
        for layer, vintage in snapshot.layer_vintages.items():
            layers.set_vintage(layer, vintage)

        # Off unless asked. A local fixture of the same name still wins, so this only
        # fills gaps - and a GDAL /vsicurl/ read bypasses the cache, the budget and
        # LONGRUN_OFFLINE, which is a hole worth keeping deliberate.
        remote = _remote_map(route, remote_rasters)
        budget = Budget()
        ctx = ScorerContext(
            layers=layers,
            rasters=FileRasterStore(root, remote=remote, offline=offline, budget=budget),
            cache=cache,
            clock=FrozenClock(start_at),
            coverage=CoverageManifest(),
            profile=profile,
            budget=budget,
            utc_offset_hours=utc_offset,
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
        # Scope 7.1: elevation comes from the terrain model, never from the GPX. Writing it
        # onto the stored route makes that true of `plan.json` too, so a later `export` or
        # `route_diff` reads the same numbers this run scored - without the DEM.
        route = route.model_copy(
            update={
                "points": [
                    point.model_copy(update={"ele_m": value})
                    for point, value in zip(route.points, elevations, strict=True)
                ]
            }
        )

        eta_vector = pacing_model(route, start_at, elevations=elevations)
        manifest = Manifest(snapshot=snapshot)
        results = _run_scorers(route, segments, ctx, eta_vector.etas, manifest)

        # Verification runs before the plan is assembled so the plan can carry its own
        # report: `plan.json` is the golden-test artifact and the scope 10.3 API contract,
        # and a verification the plan cannot report is one every later consumer has to
        # recompute from inputs it may no longer have.
        report = gpx_verify(
            route,
            request,
            segments=segments,
            results=results,
            etas=eta_vector.etas,
            elevations=elevations,
        )
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
            verify=report,
            elevation=elevation,
            manifest=manifest,
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


def _remote_map(route: Any, enabled: bool) -> dict[str, str]:
    """National raster URLs, or nothing at all when `--remote-rasters` was not given."""
    if not enabled:
        return {}
    from longrun.core.data.rasters import three_dep_url
    from longrun.core.geo.projections import centroid

    middle = centroid(route)
    return {"dem": three_dep_url(middle.lat, middle.lon)}


def _load_snapshot(path: Path | None) -> SnapshotPins:
    """Read the data-snapshot pins, or return empty ones.

    Empty is the honest default rather than an error: a run against fixtures that carry no
    recorded vintage should report no vintage, not a made-up one. What it must never do is
    report a vintage the data does not have.
    """
    if path is None:
        return SnapshotPins()
    return SnapshotPins.model_validate_json(path.read_text(encoding="utf-8"))


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
