"""`longrun refresh` - re-score a stored plan against a new date (scope 7.8, 10.1).

Advertised in `cli/__init__.py` since the first commit and built in M10. Argument parsing,
context and output; the work is `core.plan.refresh.rescore_plan`, which the `refresh_plan`
MCP tool calls too - one function, two doors.

`--date` is required rather than defaulting to today. A command that reads the wall clock is
one the golden harness cannot freeze and one whose output is not reproducible, which is scope
3.3's rule applied to a CLI. "Same date, fresher adapters" is served by passing it again.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as time_type
from pathlib import Path

import typer

from longrun.core.data.cache import offline_from_env
from longrun.core.export.sheet_md import render_markdown
from longrun.core.models.plan import Plan
from longrun.core.plan.refresh import rescore_plan
from longrun.core.scorers.freshness import rescore_set
from longrun.core.scorers.registry import SCORERS, StalePrior, UnknownScorer, closure
from longrun.runtime import open_context


def refresh(
    plan_path: Path = typer.Argument(..., help="Stored plan (plan.json) to re-score."),
    date: datetime = typer.Option(..., "--date", formats=["%Y-%m-%d"], help="New run date."),
    start: str = typer.Option("07:00", "--start", help="Start time, HH:MM."),
    out: Path | None = typer.Option(
        None, "--out", help="Write here instead of over the stored plan."
    ),
    only: str | None = typer.Option(
        None, "--only", help="Comma-separated scorers to re-run, instead of the usual set."
    ),
    every: bool = typer.Option(False, "--all", help="Re-run every scorer, carrying none."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Say what would be re-run and carried, then stop."
    ),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    cache_path: Path | None = typer.Option(None, "--cache", help="Cache/cassette file."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    remote_rasters: bool = typer.Option(
        False, "--remote-rasters", help="Read 3DEP and canopy over the network."
    ),
    resample_elevation: bool = typer.Option(
        False,
        "--resample-elevation",
        help="Re-read the DEM instead of keeping the stored profile.",
    ),
    fail_on_new_hard: bool = typer.Option(
        False, "--fail-on-new-hard", help="Exit 1 if a hard flag appeared since the plan."
    ),
) -> None:
    """Re-score the date-sensitive scorers and carry the rest."""
    if only and every:
        typer.echo("error: choose one of --only or --all", err=True)
        raise typer.Exit(code=2)

    try:
        stored = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"error: could not read {plan_path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        hour, _, minute = start.partition(":")
        start_at = datetime.combine(date.date(), time_type(int(hour), int(minute or 0)))
    except ValueError as exc:
        typer.echo(f"error: could not read --start {start!r}; expected HH:MM", err=True)
        raise typer.Exit(code=2) from exc

    if every:
        requested = set(SCORERS)
    elif only:
        requested = {name.strip() for name in only.split(",") if name.strip()}
    else:
        requested = set(rescore_set())

    # Widened here rather than inside `run_scorers`, so the widening can be *reported*: it
    # refuses an unclosed set precisely so a caller with a terminal says what it added.
    try:
        wanted = closure(requested)
    except (StalePrior, UnknownScorer) as exc:  # pragma: no cover - closure does not raise
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    unknown = sorted(wanted - set(SCORERS))
    if unknown:
        typer.echo(f"error: no scorer answers to {unknown}", err=True)
        raise typer.Exit(code=2)
    if added := sorted(wanted - requested):
        typer.echo(f"also re-scoring {', '.join(added)}, which a requested scorer reads")

    carried = [name for name in SCORERS if name not in wanted]
    if dry_run:
        typer.echo(f"would re-score {len(wanted)}: {', '.join(sorted(wanted))}")
        typer.echo(f"would carry {len(carried)}: {', '.join(carried)}")
        return

    with open_context(
        route=stored.route,
        root=fixtures or Path("data"),
        snapshot=stored.manifest.snapshot,
        start_at=start_at,
        profile=stored.profile,
        offline=offline or offline_from_env(),
        cache_path=cache_path,
        remote_rasters=remote_rasters,
        utc_offset=stored.request.utc_offset_hours,
        # A refresh keeps sweeping the window the plan was asked for. Dropping it here
        # would make `longrun refresh` quietly answer a different question from the one
        # `longrun plan` answered about the same route.
        start_window=stored.request.start_window,
    ) as ctx:
        refreshed = rescore_plan(
            stored,
            ctx,
            start_at=start_at,
            only=wanted,
            resample_elevation=resample_elevation,
        )

    destination = out or plan_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(refreshed.plan.model_dump_json(indent=2), encoding="utf-8")
    if out:
        (out.parent / "sheet.md").write_text(render_markdown(refreshed.plan), encoding="utf-8")

    for line in refreshed.delta.lines():
        typer.echo(line)
    typer.echo(f"wrote {destination}")

    if fail_on_new_hard and refreshed.delta.new_hard_flags:
        typer.echo(f"new hard flag(s): {', '.join(refreshed.delta.new_hard_flags)}", err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.command("refresh")(refresh)


__all__ = ["refresh", "register"]
