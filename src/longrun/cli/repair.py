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
    "hazards": "longrun.core.scorers.hazards",
    # The three adapter-fed scorers (scope 7.6, 7.10). Grouped because they resolve the
    # same jurisdictions and ask the same registry; `closures` is first because it is the
    # only one that can fail verification.
    "closures": "longrun.core.scorers.closures",
    "trail_status": "longrun.core.scorers.trail_status",
    "access_hours": "longrun.core.scorers.access_hours",
    "transit": "longrun.core.scorers.transit",
    # After transit: both read the same stop layer, and `bailouts` reuses its service test.
    "bailouts": "longrun.core.scorers.bailouts",
    "crew_points": "longrun.core.scorers.crew_points",
    # Last, and deliberately: it re-evaluates what the time-dependent scorers above
    # measured, at a dozen other start times, from the horizons they already built.
    "start_time_optimizer": "longrun.core.scorers.start_time_optimizer",
    "cell_coverage": "longrun.core.scorers.cell_coverage",
}

#: Scorers the scope calls for whose milestone has not arrived. Named explicitly so the
#: coverage manifest can say they were not run, instead of the sheet staying silent.
#:
#: **Empty since M4**, and kept rather than deleted. Two modules import it, and the mechanism
#: is the honest one for a scorer the scope names and this build cannot run - it is how
#: `closures`, `trail_status` and `access_hours` were reported from M1 until M4.4 wrote them.
#: The next scorer the scope names and a milestone defers belongs here, not in a comment.
NOT_YET_IMPLEMENTED: dict[str, str] = {}


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

    score_route(
        route,
        request,
        start_at=start_at,
        profile=profile,
        root=root,
        snapshot=snapshot,
        offline=offline,
        cache_path=cache_path,
        remote_rasters=remote_rasters,
        utc_offset=utc_offset,
        out=out,
    )


def score_route(
    route: Any,
    request: PlanRequest,
    *,
    start_at: datetime,
    profile: Any,
    root: Path,
    snapshot: SnapshotPins,
    offline: bool = False,
    cache_path: Path | None = None,
    remote_rasters: bool = False,
    utc_offset: float | None = None,
    out: Path | None = None,
    router: Any = None,
) -> Plan:
    """Score a route and render its sheet. The one pipeline both modes run through.

    Extracted from `repair` when `plan` arrived, so generate mode and repair mode are the
    same code from the route onwards. Two entry points that each scored a route their own
    way would drift, and the golden suite only watches one of them.

    `router` is the only difference between the modes and it changes exactly one thing:
    where way ids come from. Repair mode has none and snaps geometrically
    (`core.geo.matching`); generate mode has the router that drew the route, so
    `POST /match` returns real `osm_way_id` values per edge - risk R4's finding, and what
    scope 6.2's accepted-road set is built from.
    """
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
        # Imported here rather than at module scope so `longrun --version` does not pay for
        # entry-point scanning, and so a third-party adapter that will not import cannot
        # break a command that never asked for one. `discover()` reports rather than raises,
        # but the import itself is still work nobody asked for on most invocations.
        from longrun.adapters.registry import AdapterRegistry

        ctx = ScorerContext(
            layers=layers,
            rasters=FileRasterStore(root, remote=remote, offline=offline, budget=budget),
            cache=cache,
            clock=FrozenClock(start_at),
            coverage=CoverageManifest(),
            profile=profile,
            budget=budget,
            snapshot=snapshot.layer_vintages,
            features=AdapterRegistry(cache, budget, offline=offline),
            utc_offset_hours=utc_offset,
        )

        # Where way ids come from is the one thing the two modes do differently.
        #
        # Generate mode has the router that drew the route, so `POST /match` gives real
        # `osm_way_id` values per edge - risk R4's finding, and the only source scope 6.2's
        # accepted-road set can be built from. Repair mode has no router by design (scope
        # 6.1), so it snaps geometrically instead; without that every way-tag scorer reads
        # `unknown` for the whole route and the commonest entry mode is the least useful.
        matched = _matched_way_ids(route, router) if router is not None else None
        if matched is not None:
            # The matcher returns the route **snapped to the graph**, which is a different
            # point list from the one it was given - 268 points for 228 in, on a route the
            # same router had just drawn. That snapped geometry is the answer, not a
            # by-product: it is the one that is provably on the network, which is what
            # check 2 asks and what scope 6.2's accepted-road set is built from.
            route, way_ids = matched
            known = sum(1 for w in way_ids if w is not None)
            segments = segment_route(route, way_ids=way_ids)
            ctx.coverage.record(
                CoverageEntry(
                    source="way_matching",
                    kind="osm_tags",
                    checked=True,
                    reason=f"{known} of {len(way_ids)} points matched by the router",
                    confidence=round(known / len(way_ids), 3),
                )
            )
            # Zero for **every** point, including the ones with no way id. The matched
            # geometry is edge geometry: `/match` returns the path *through the graph*, so
            # each of its points lies on a way whether or not an `osm_way_id` detail range
            # happened to cover it - the ranges are half-open and leave gaps at junctions.
            #
            # Encoding those gaps as `inf` instead made 88 of 268 points check-2 offenders
            # on a route the router had just drawn, which is scope 12's mistake in
            # miniature: unknown reported as bad.
            snapped: list[float] | None = [0.0] * len(route.points)
        else:
            match = _match_ways(route, ctx)
            segments = segment_route(route, way_ids=match.way_ids if match else None)
            snapped = list(match.distances_m) if match is not None else None
            if match is not None:
                ctx.coverage.record(_matching_coverage(match))

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
            # Check 2 has skipped on every plan since M1.7, and not for want of the data:
            # `assign_way_ids` has returned a per-point snap distance all along and nobody
            # passed it here. "No map-matching result available" was true of the argument,
            # not of the run.
            snapped_distances_m=snapped,
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

    return plan


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


def _matching_coverage(match: Any) -> CoverageEntry:
    """Report how much of the route carries way tags at all (scope 3.6, 12).

    Recorded on **every** run, and that is a correction rather than a tidy-up. Until the
    OSM loader landed there was no real corridor to notice it on, and the entry was written
    only when `is_usable` was False — below 50%. The first real corridor snapped 62% of its
    points, so 38% of the route had no tags, every tag-driven scorer quietly described the
    other 62%, and the sheet said nothing at all.

    That is precisely the failure mode scope 12 names: a partial answer presented as a
    whole one. A silence at 62% and a warning at 49% is a cliff no reader can see.
    """
    rate = match.match_rate
    if rate >= 1.0:
        return CoverageEntry(
            source="way_matching",
            kind="osm_tags",
            checked=True,
            reason=None,
            confidence=1.0,
        )
    unmatched = len(match.way_ids) - match.matched
    tail = (
        "tag-driven scorers cover a minority of this route"
        if not match.is_usable
        else f"{unmatched} point(s) carry no way tags"
    )
    return CoverageEntry(
        source="way_matching",
        kind="osm_tags",
        checked=match.is_usable,
        reason=(
            f"{rate:.0%} of route points snapped to a way within {match.tolerance_m:.0f} m; {tail}"
        ),
        confidence=round(rate, 3),
    )


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


def _matched_way_ids(route: Any, router: Any) -> tuple[Any, list[int | None]] | None:
    """The snapped route and its per-point way ids, or None when the matcher could not say.

    Returns both, because the matcher's answer is a *different route*: it snaps each point
    onto the graph and returns the resulting geometry, which has its own point count. Using
    the ids against the original points would put every tag on the wrong point.

    None rather than a list of Nones, so the caller falls back to geometric snapping: a
    matcher that returned nothing is a reason to use the fallback, not a reason to score a
    route with no tags at all.
    """
    try:
        matched, way_ids = router.map_match(route)
    except Exception:  # noqa: BLE001 - matching is optional; the route is not
        return None
    if not way_ids or len(way_ids) != len(matched.points) or not any(way_ids):
        return None
    return matched, list(way_ids)
