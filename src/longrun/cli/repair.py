"""`longrun repair` - score a user-supplied GPX and emit a plan sheet (scope 6.1, 10.1).

Repair mode is the entry path scope 6.1 expects experienced users to take, and scope 8.1
step 3 lets it skip straight to scoring: the user supplies the geometry, so no router is
required. That makes this the first end-to-end command, and per scope 10.1 it is also the
golden-test harness - every golden route runs through here with no model in the loop.

What this module is, after M5.1, is argument parsing and output. The scoring pass lives in
`core.plan.pipeline`, the scorer list in `core.scorers.registry`, and the context in
`longrun.runtime` - because scope 8.1 step 6 runs the same pass several times against one
open context, and a pass that built its own context could not be reused that way.
"""

from __future__ import annotations

import sys
from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any

import typer

from longrun.core.data.cache import offline_from_env
from longrun.core.export.sheet_md import render_markdown
from longrun.core.geo.gpx import GpxError, gpx_read
from longrun.core.models.plan import Manifest, Plan, SnapshotPins
from longrun.core.models.request import PlanRequest
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.preferences.store import load_profile
from longrun.runtime import open_context


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
    cache: Any = None,
    budget: Any = None,
) -> Plan:
    """Score one route and render its sheet. The one pipeline both modes run through.

    Extracted from `repair` when `plan` arrived, so generate mode and repair mode are the
    same code from the route onwards. Two entry points that each scored a route their own
    way would drift, and the golden suite only watches one of them.

    Since M5.1 this is a thin composition of three pieces - `open_context`, `score_once`,
    `build_plan` - and what it still owns is rendering and writing, which is the half that
    cannot move into `core/` because it speaks `typer`.
    """
    with open_context(
        route=route,
        root=root,
        snapshot=snapshot,
        start_at=start_at,
        profile=profile,
        offline=offline,
        cache_path=cache_path,
        remote_rasters=remote_rasters,
        utc_offset=utc_offset,
        cache=cache,
        budget=budget,
    ) as ctx:
        manifest = Manifest(snapshot=snapshot)
        scored = score_once(
            route, request, ctx, start_at=start_at, router=router, manifest=manifest
        )
        plan = build_plan(
            scored, request, profile=profile, coverage=scored.coverage, manifest=manifest
        )
        sheet = render_markdown(
            plan,
            elevation=scored.elevation,
            verify=scored.verify,
            pacing_caveats=scored.caveats,
        )

        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / "sheet.md").write_text(sheet, encoding="utf-8")
            (out / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            typer.echo(f"wrote {out / 'sheet.md'} and {out / 'plan.json'}")
        else:
            _echo_utf8(sheet)

    return plan


def _load_snapshot(path: Path | None) -> SnapshotPins:
    """Read the data-snapshot pins, or return empty ones.

    Empty is the honest default rather than an error: a run against fixtures that carry no
    recorded vintage should report no vintage, not a made-up one. What it must never do is
    report a vintage the data does not have.
    """
    if path is None:
        return SnapshotPins()
    return SnapshotPins.model_validate_json(path.read_text(encoding="utf-8"))


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


__all__ = ["register", "repair", "score_route"]
