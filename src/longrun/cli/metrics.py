"""`longrun metrics plan.json` - scope 7.1's acceptance metrics for a stored plan.

> Acceptance metrics: fraction of length at LTS >=3, count of LTS 4 segments, detour ratio
> vs. shortest legal route, **published for a set of reference routes**.

The emphasis is the reason this is a command rather than something every plan does. The
first two metrics are free: `segment_hostility` has written them into its route summary
since M1 and nothing has ever read them, so `longrun plan` and `longrun repair` now carry
them on `Plan.metrics` at no cost. The third needs a second routing call, and a plan has
200 of those and 180 seconds. So the detour ratio is measured when somebody asks for it, on
the routes scope 7.1 says to publish - which is what `--router` does here.

Reads a stored `plan.json` rather than re-scoring, like `longrun export`: the LTS numbers
are already in it, and re-deriving them would mean re-reading layers the plan may no longer
have. The only thing this needs a live service for is the denominator.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from longrun.core.models.plan import Plan
from longrun.core.plan.metrics import acceptance_metrics, metrics_with_router


def metrics(
    plan_file: Path = typer.Argument(..., help="A stored plan.json."),
    router_url: str | None = typer.Option(
        None,
        "--router",
        help="GraphHopper URL. Without it the detour ratio is reported as not measured.",
    ),
    write: bool = typer.Option(False, "--write", help="Write the metrics back into the plan file."),
    as_json: bool = typer.Option(False, "--json", help="Print the metrics as JSON."),
) -> None:
    """Publish scope 7.1's acceptance metrics for a stored plan."""
    if not plan_file.is_file():
        typer.echo(f"error: no such plan file: {plan_file}", err=True)
        raise typer.Exit(code=2)

    try:
        plan = Plan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        typer.echo(f"error: {plan_file} is not a plan: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if router_url:
        # Imported here rather than at module scope: `longrun --version` should not pay
        # for httpx, which is the rule every other command in this package follows.
        from longrun.core.routing.graphhopper import GraphHopperRouter

        measured = metrics_with_router(plan.route, plan.results, GraphHopperRouter(router_url))
    else:
        measured = acceptance_metrics(
            plan.route, plan.results, reasons=["no --router given, so nothing was routed"]
        )

    if as_json:
        typer.echo(json.dumps(measured.as_dict(), indent=2))
    else:
        typer.echo(str(measured))
        for reason in measured.reasons:
            typer.echo(f"  {reason}")

    if write:
        updated = plan.model_copy(update={"metrics": measured.as_dict()})
        plan_file.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"wrote {plan_file}")


def register(app: typer.Typer) -> None:
    app.command("metrics")(metrics)


__all__ = ["metrics", "register"]
