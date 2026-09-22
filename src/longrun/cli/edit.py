"""`longrun edit` — scope 10.3's five gestures, before any map control exists (scope 3.9).

Scope 3.9's rule is that a capability exists as a tool and a command before it gets a UI
control, and §10.3 names five: lock a range, unlock one, choose an alternative for a flagged
stretch, drag in a via point, draw an avoid polygon. This is all five, and it is also how
M11 is tested at all — a headless agent cannot drag on a MapLibre canvas, and a milestone
whose only door was a browser would ship unverified.

**Every command operates on a stored `plan.json`**, and that is the choice worth stating.
The alternative is the scratchpad, which is where the loop resumes from — but the
scratchpad is written only by the agent loop, `longrun repair` produces none, and
`rescore` needs the plan's stored *measurements* to carry anyway. A plan carries the
request (locks, vias, avoid polygons), the line, the segmentation and the results, which is
every input these five need. The loop picks locks and vias back up through
`PlanRequest`, which is where `_fresh` reads them from.

**Three of the five change the request and leave the line where it is**, and they say so
rather than implying a route that honours them. Redrawing is a routing call, it is
`reroute`'s job, and a via nobody has routed through is a via `gpx_verify` will report. The
two that change the line — `choose` and `reroute` — re-score in the same command, because a
plan carrying an edited line and measurements of the old one is the state this milestone
exists to prevent.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any

import typer

from longrun.core.data.cache import offline_from_env
from longrun.core.export.sheet_md import render_markdown
from longrun.core.geo.gpx import gpx_read
from longrun.core.models.geometry import LatLon
from longrun.core.models.plan import Plan
from longrun.core.plan.edits import NotAnEdit, insert_via, lock_range, splice, unlock_range
from longrun.core.plan.refresh import rescore_plan
from longrun.core.routing.avoid import area_from_polygon
from longrun.core.scorers.registry import SCORERS, StalePrior, UnknownScorer, closure
from longrun.runtime import open_context

edit = typer.Typer(
    name="edit",
    help="Scope 10.3's direct manipulation, against a stored plan.",
    no_args_is_help=True,
)


def _load(path: Path) -> Plan:
    try:
        return Plan.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"error: could not read {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _write(plan: Plan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"wrote {path}")


def _point(text: str) -> LatLon:
    try:
        lat, _, lon = text.partition(",")
        return LatLon(lat=float(lat), lon=float(lon))
    except (TypeError, ValueError) as exc:
        typer.echo(f"error: could not read {text!r}; expected lat,lon", err=True)
        raise typer.Exit(code=2) from exc


@edit.command("lock")
def lock_command(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json)."),
    start_m: float = typer.Option(..., "--from-m", help="Where the locked range begins."),
    end_m: float = typer.Option(..., "--to-m", help="Where it ends."),
    reason: str | None = typer.Option(None, "--reason", help="Why, for the sheet."),
) -> None:
    """Exclude a range from rerouting (scope 6.4, 10.3)."""
    plan = _load(plan_path)
    locked = lock_range(plan.request.locked, start_m, end_m, reason, source="user")
    edited = plan.model_copy(update={"request": plan.request.model_copy(update={"locked": locked})})
    typer.echo(f"locked {start_m:.0f}-{end_m:.0f} m; {len(locked)} lock(s) on this plan")
    _write(edited, plan_path)


@edit.command("unlock")
def unlock_command(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json)."),
    start_m: float = typer.Option(..., "--from-m", help="Where the released range begins."),
    end_m: float = typer.Option(..., "--to-m", help="Where it ends."),
    mine: bool = typer.Option(
        False, "--mine", help="Only release locks the runner set, leaving the loop's standing."
    ),
) -> None:
    """Release a locked range, trimming a lock it only partly covers (scope 10.3)."""
    plan = _load(plan_path)
    try:
        kept, freed = unlock_range(
            plan.request.locked, start_m, end_m, source="user" if mine else None
        )
    except NotAnEdit as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not freed:
        typer.echo(f"nothing locked in {start_m:.0f}-{end_m:.0f} m")
    for lock in freed:
        typer.echo(
            f"released {lock.start_m:.0f}-{lock.end_m:.0f} m "
            f"({lock.source}: {lock.reason or 'no reason given'})"
        )
    edited = plan.model_copy(update={"request": plan.request.model_copy(update={"locked": kept})})
    _write(edited, plan_path)


@edit.command("via")
def via_command(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json)."),
    at: str = typer.Option(..., "--at", help="The via point as lat,lon."),
    at_m: float | None = typer.Option(
        None, "--at-m", help="Where along the line it belongs, if not where the point is."
    ),
) -> None:
    """Add a via point to the request (scope 7.8's `pin_waypoint`, scope 10.3's drag).

    The line is left alone. Drawing one through the new point is a routing call - see
    `longrun edit reroute` - and until it happens `gpx_verify` is what reports that the
    stored route does not visit it.
    """
    plan = _load(plan_path)
    via, index = insert_via(plan.request.via, _point(at), plan.route, at_m)
    edited = plan.model_copy(update={"request": plan.request.model_copy(update={"via": via})})
    typer.echo(f"via {index + 1} of {len(via)}; the stored line still runs where it did")
    typer.echo("re-route to draw through it: longrun edit reroute")
    _write(edited, plan_path)


@edit.command("avoid")
def avoid_command(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json)."),
    polygon_path: Path = typer.Option(..., "--polygon", help="GeoJSON polygon to stay out of."),
) -> None:
    """Add an avoid polygon to the request (scope 6.4, 10.3).

    The polygon is rounded at `AREA_PRECISION` and size-checked **here**, before it is
    stored, for the reasons `avoid.area_from_polygon` gives: an unrounded coordinate poisons
    the routing cache key, and an over-cap area closes the parallel streets a detour was
    going to use. An area that is refused is refused by name with its size, never dropped.
    """
    plan = _load(plan_path)
    try:
        raw = json.loads(polygon_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"error: could not read {polygon_path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    area, refusal = area_from_polygon(raw, area_id=f"avoid-{len(plan.request.avoid_polygons)}")
    if area is None:
        typer.echo(f"error: {refusal}", err=True)
        raise typer.Exit(code=2)

    polygons = [*plan.request.avoid_polygons, area]
    edited = plan.model_copy(
        update={"request": plan.request.model_copy(update={"avoid_polygons": polygons})}
    )
    typer.echo(f"{len(polygons)} avoid area(s) on this plan")
    # `RoutingPolicy` is frozen and resolved once, persisted so a resume in another process
    # cannot compute a different one. The honest answer to "avoid this now" is a re-route,
    # not a patch to a policy the first half of the line was already drawn without.
    typer.echo("the stored line was drawn without it; re-route to honour it")
    _write(edited, plan_path)


@edit.command("choose")
def choose_command(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json)."),
    start_m: float = typer.Option(..., "--from-m", help="Where the replaced stretch begins."),
    end_m: float = typer.Option(..., "--to-m", help="Where it ends."),
    alternative: Path = typer.Option(..., "--alternative", help="GPX of the line to use instead."),
    date: datetime | None = typer.Option(
        None, "--date", formats=["%Y-%m-%d"], help="Date to re-score against. Default: the plan's."
    ),
    start: str | None = typer.Option(None, "--start", help="Start time, HH:MM."),
    only: str | None = typer.Option(
        None, "--only", help="Comma-separated scorers to re-run; the rest are carried."
    ),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    cache_path: Path | None = typer.Option(None, "--cache", help="Cache/cassette file."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    out: Path | None = typer.Option(None, "--out", help="Write here instead of over the plan."),
) -> None:
    """Replace a flagged stretch with an alternative, auto-lock it, and re-score (scope 10.3).

    The splice and the re-score are one command on purpose. A plan carrying an edited line
    and measurements of the old one is exactly the state M11 exists to prevent, and leaving
    the two as separate steps would make it reachable by stopping halfway.

    The chosen stretch auto-locks (scope 7.8, ADR 0019) as the runner's own: they chose it,
    so `longrun edit unlock --mine` can take it back, and the loop's next round will not
    reopen it.
    """
    plan = _load(plan_path)
    try:
        line = splice(plan.route, start_m, end_m, gpx_read(str(alternative)))
    except NotAnEdit as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    locked = lock_range(
        plan.request.locked, start_m, end_m, reason="chose an alternative", source="user"
    )
    stored = plan.model_copy(update={"request": plan.request.model_copy(update={"locked": locked})})
    typer.echo(
        f"spliced {start_m:.0f}-{end_m:.0f} m: "
        f"{plan.route.length_m / 1000:.2f} -> {line.length_m / 1000:.2f} km, and locked it"
    )
    _rescore(stored, line, plan_path, date, start, only, fixtures, cache_path, offline, out)


@edit.command("reroute")
def reroute_command(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json)."),
    date: datetime | None = typer.Option(
        None, "--date", formats=["%Y-%m-%d"], help="Date to re-score against. Default: the plan's."
    ),
    start: str | None = typer.Option(None, "--start", help="Start time, HH:MM."),
    only: str | None = typer.Option(
        None, "--only", help="Comma-separated scorers to re-run; the rest are carried."
    ),
    router_url: str | None = typer.Option(None, "--router", help="GraphHopper base URL."),
    region: str | None = typer.Option(None, "--region", help="Region whose graph to route on."),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    cache_path: Path | None = typer.Option(None, "--cache", help="Cache/cassette file."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    out: Path | None = typer.Option(None, "--out", help="Write here instead of over the plan."),
) -> None:
    """Draw the line again through the request as it now stands, and re-score it.

    This is how `via` and `avoid` take effect, and it is a whole re-route rather than a patch
    because `RoutingPolicy` is frozen and resolved once - persisted so that a resume in
    another process cannot compute a different one. Adding an avoid area to the policy the
    first half of a line was already drawn without would cost the invariant and buy a line
    that is half one thing and half another.
    """
    from longrun.core.data.cache import SqliteCache, cache_path_from_env
    from longrun.core.models.context import Budget
    from longrun.core.plan.edits import redraw
    from longrun.core.plan.scratchpad import Scratchpad
    from longrun.core.routing.base import NoRouteError, RouterUnavailable
    from longrun.core.routing.cached import CachedRouter
    from longrun.core.routing.graphhopper import GraphHopperRouter
    from longrun.regions.routers import resolve
    from longrun.runtime import PLAN_LATENCY_BUDGET_S

    plan = _load(plan_path)
    if plan.request.start is None or plan.request.end is None:
        typer.echo("error: this plan's request has no start and end to route between", err=True)
        raise typer.Exit(code=2)

    waypoints = [plan.request.start, *plan.request.via, plan.request.end]
    # A scratchpad is what `redraw` reads, and the one this builds is a carrier rather than a
    # resume point: the policy is the plan's own, so the new line is drawn with the costing
    # model the old one was, plus whatever areas the request has grown since.
    pad = Scratchpad(plan_id=plan.id, request=plan.request, route=plan.route)
    with SqliteCache(cache_path or cache_path_from_env(), offline=offline) as cache:
        budget = Budget(latency_budget_s=PLAN_LATENCY_BUDGET_S)
        chosen = resolve(waypoints, region=region, explicit=router_url)
        router = CachedRouter(
            GraphHopperRouter(chosen.url),
            cache,
            budget,
            graph=plan.manifest.snapshot.graph_identity,
        )
        typer.echo(f"routing {len(waypoints)} point(s) through {chosen.note}")
        try:
            line = redraw(pad, router)
        except (RouterUnavailable, NoRouteError, NotAnEdit) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=3) from exc

    typer.echo(f"redrew {plan.route.length_m / 1000:.2f} -> {line.length_m / 1000:.2f} km")
    _rescore(plan, line, plan_path, date, start, only, fixtures, cache_path, offline, out)


def _rescore(
    stored: Plan,
    line: Any,
    plan_path: Path,
    date: datetime | None,
    start: str | None,
    only: str | None,
    fixtures: Path | None,
    cache_path: Path | None,
    offline: bool,
    out: Path | None,
) -> None:
    """Score the edited line against the stored plan, and say what the carry cost.

    Shared by `choose` and `reroute` because they differ only in where the new line came
    from. The stored plan's own date stands in when none is given: an edit asks "what is
    this line now", not "what will it be in March", and silently moving the date would
    change every time-dependent answer under cover of a geometry change.
    """
    start_at = _start_at(stored, date, start)
    wanted = _wanted(only)
    if wanted is not None and only is not None:
        asked = {name.strip() for name in only.split(",") if name.strip()}
        if added := sorted(wanted - asked):
            typer.echo(f"also re-scoring {', '.join(added)}, which a requested scorer reads")

    with open_context(
        route=line,
        root=fixtures or Path("data"),
        snapshot=stored.manifest.snapshot,
        start_at=start_at,
        profile=stored.profile,
        offline=offline or offline_from_env(),
        cache_path=cache_path,
        utc_offset=stored.request.utc_offset_hours,
        start_window=stored.request.start_window,
    ) as ctx:
        rescored = rescore_plan(stored, ctx, start_at=start_at, route=line, only=wanted)

    for report in rescored.delta.lines():
        typer.echo(report)
    destination = out or plan_path
    _write(rescored.plan, destination)
    if out:
        (out.parent / "sheet.md").write_text(render_markdown(rescored.plan), encoding="utf-8")


def _start_at(stored: Plan, date: datetime | None, start: str | None) -> datetime:
    day = date.date() if date is not None else stored.request.date
    if start is None:
        clock = stored.etas[0].time() if stored.etas else (stored.request.start_time or time_type())
        return datetime.combine(day, clock)
    try:
        hour, _, minute = start.partition(":")
        return datetime.combine(day, time_type(int(hour), int(minute or 0)))
    except ValueError as exc:
        typer.echo(f"error: could not read --start {start!r}; expected HH:MM", err=True)
        raise typer.Exit(code=2) from exc


def _wanted(only: str | None) -> set[str] | None:
    """The scorers to re-run, closed over what they read - or None for every one of them.

    Widened here rather than inside `run_scorers`, so the widening can be *reported*: it
    refuses an unclosed set precisely so that a caller with a terminal says what it added.
    """
    if not only:
        return None
    requested = {name.strip() for name in only.split(",") if name.strip()}
    try:
        wanted = closure(requested)
    except (StalePrior, UnknownScorer) as exc:  # pragma: no cover - closure does not raise
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if unknown := sorted(wanted - set(SCORERS)):
        typer.echo(f"error: no scorer answers to {unknown}", err=True)
        raise typer.Exit(code=2)
    return set(wanted)


def register(app: typer.Typer) -> None:
    app.add_typer(edit)


__all__ = ["edit", "register"]
