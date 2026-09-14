"""`longrun jobs` and `longrun resume` - the other half of a `needs_input` pause.

Scope 4.2 designs the pause to survive a process boundary, so there has to be a way back
in from a fresh process. `longrun plan` writes the scratchpad and exits 6; these read it,
record the answer, and set the loop going again from the round it stopped on.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any

import typer

from longrun.core.data.cache import (
    SqliteCache,
    cache_path_from_env,
    offline_from_env,
)
from longrun.core.models.context import Budget
from longrun.core.models.plan import SnapshotPins
from longrun.core.preferences.store import load_profile
from longrun.jobs.runner import JobStore

DEFAULT_JOBS_DIR = Path("data") / "jobs"


def jobs(
    directory: Path = typer.Option(DEFAULT_JOBS_DIR, "--dir", help="Where scratchpads live."),
) -> None:
    """List stored jobs and what each is waiting for."""
    store = JobStore(directory)
    ids = store.ids()
    if not ids:
        typer.echo(f"no jobs in {directory}")
        return
    for job_id in ids:
        pad = store.load(job_id)
        waiting = f" - {pad.question.prompt}" if pad.question is not None else ""
        typer.echo(f"{job_id}  {pad.status:<11} round {pad.round}{waiting}")


def resume(
    job_id: str = typer.Argument(..., help="Job to continue."),
    choose: str = typer.Option(..., "--choose", help="The option to take."),
    directory: Path = typer.Option(DEFAULT_JOBS_DIR, "--dir", help="Where scratchpads live."),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    snapshot_path: Path | None = typer.Option(None, "--snapshot", help="Data-snapshot pins."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Preference profile YAML."),
    router_url: str | None = typer.Option(None, "--router", help="GraphHopper base URL."),
    cache_path: Path | None = typer.Option(None, "--cache", help="Cache/cassette file."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    out: Path | None = typer.Option(None, "--out", help="Directory for outputs."),
) -> None:
    """Answer what a parked job asked, and let it finish."""
    from longrun.agent.loop import answer, plan_route
    from longrun.core.export.sheet_md import render_markdown
    from longrun.core.routing.cached import CachedRouter
    from longrun.core.routing.graphhopper import GraphHopperRouter
    from longrun.runtime import PLAN_LATENCY_BUDGET_S, open_context

    store = JobStore(directory)
    try:
        pad = store.load(job_id)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: no job {job_id} in {directory}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if pad.question is None:
        typer.echo(f"error: job {job_id} is {pad.status} and is not waiting on anything", err=True)
        raise typer.Exit(code=2)
    if choose not in pad.question.options:
        typer.echo(
            f"error: {choose!r} is not one of {pad.question.options}",
            err=True,
        )
        raise typer.Exit(code=2)

    answer(pad, choose)
    store.save(pad)

    if pad.route is None:  # pragma: no cover - a parked job has always routed
        typer.echo("error: the stored job has no route to continue from", err=True)
        raise typer.Exit(code=2)

    snapshot = _snapshot(snapshot_path)
    offline = offline or offline_from_env()
    start_at = _start_at(pad)

    with SqliteCache(cache_path or cache_path_from_env(), offline=offline) as cache:
        budget = Budget(latency_budget_s=PLAN_LATENCY_BUDGET_S)
        router = CachedRouter(
            GraphHopperRouter(router_url), cache, budget, graph=snapshot.osm_extract_date
        )
        with open_context(
            route=pad.route,
            root=fixtures or Path("data"),
            snapshot=snapshot,
            start_at=start_at,
            profile=load_profile(profile_path),
            offline=offline,
            cache=cache,
            budget=budget,
        ) as ctx:
            outcome = plan_route(
                pad.request,
                ctx,
                start_at=start_at,
                route=pad.route,
                router=router,
                resume=pad,
            )

    store.save(outcome.scratchpad)
    typer.echo(f"job {job_id}: {outcome.scratchpad.status}, round {outcome.scratchpad.round}")

    if outcome.needs_input and outcome.scratchpad.question is not None:
        typer.echo(f"needs input: {outcome.scratchpad.question.prompt}")
        for option in outcome.scratchpad.question.options:
            typer.echo(f"  - {option}")
        raise typer.Exit(code=6)

    if outcome.plan is not None and out:
        out.mkdir(parents=True, exist_ok=True)
        sheet = render_markdown(
            outcome.plan,
            elevation=outcome.scored.elevation if outcome.scored else None,
            verify=outcome.scored.verify if outcome.scored else None,
        )
        (out / "sheet.md").write_text(sheet, encoding="utf-8")
        (out / "plan.json").write_text(outcome.plan.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"wrote {out / 'sheet.md'} and {out / 'plan.json'}")


def _snapshot(path: Path | None) -> SnapshotPins:
    if path is None:
        return SnapshotPins()
    return SnapshotPins.model_validate_json(path.read_text(encoding="utf-8"))


def _start_at(pad: Any) -> datetime:
    """The plan's own start time, read back from the request it was made with.

    Not the wall clock: resuming a plan tomorrow must not re-score it for tomorrow, or the
    forecast keys change under it and the cassette stops answering.
    """
    when = pad.request.start_time or time_type(7, 0)
    return datetime.combine(pad.request.date, when)


def register(app: typer.Typer) -> None:
    app.command("jobs")(jobs)
    app.command("resume")(resume)


__all__ = ["jobs", "register", "resume"]
