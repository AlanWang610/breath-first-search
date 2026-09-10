"""`longrun export` — a stored plan into a shareable format (scope 9, 10.1).

Scope §10.1 names `longrun export plan.json --fit`. This is that command with `--html` and
`--md`; FIT and TCX are deliberately out of M2 and will slot in beside them.

It reads a **stored plan** rather than rescoring. That is what makes it useful — the DEM,
the fixtures and the forecast cassette a plan was scored against may all be gone, and
`plan.json` still has to render — and it is the same property `refresh_plan` and
`route_diff` will need. It is also a real test of whether the plan model is complete: if a
sheet cannot be drawn from the file alone, something the sheet needs was never stored.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from longrun.core.export.sheet_html import render_html
from longrun.core.export.sheet_md import render_markdown
from longrun.core.models.plan import Plan


def export(
    plan_path: Path = typer.Argument(..., help="A plan.json written by `longrun repair`."),
    out: Path | None = typer.Option(None, "--out", help="File to write; stdout if omitted."),
    html: bool = typer.Option(False, "--html", help="Self-contained HTML sheet."),
    markdown: bool = typer.Option(False, "--md", help="Markdown sheet."),
) -> None:
    """Render a stored plan as a plan sheet."""
    if html and markdown:
        typer.echo("error: choose one of --html or --md", err=True)
        raise typer.Exit(code=2)

    try:
        plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"error: could not read {plan_path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if html:
        text = render_html(plan)
    else:
        # Markdown is the default because it is the format the golden tests read and the
        # one that survives being pasted into a message.
        text = render_markdown(plan, elevation=plan.elevation, verify=plan.verify)

    if out is None:
        typer.echo(text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out} ({len(text.encode('utf-8')) / 1024:.0f} kB)")


def summary(
    plan_path: Path = typer.Argument(..., help="A plan.json written by `longrun repair`."),
) -> None:
    """One line per scorer: what it measured, what it flagged, and how sure it is.

    The quickest way to see whether a run did what it should have, without reading a sheet.
    """
    plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    unchecked = {e.source for e in plan.coverage.unchecked()}

    for result in sorted(plan.results, key=lambda r: r.name):
        hard = sum(1 for f in result.flags if f.kind.name == "HARD")
        confidences = [m.confidence for m in result.measurements]
        mean = sum(confidences) / len(confidences) if confidences else 0.0
        state = "unavailable" if result.name in unchecked and not result.measurements else "ran"
        typer.echo(
            f"{result.name:<20} {state:<12} {len(result.measurements):>3} measurement(s)  "
            f"{len(result.flags):>2} flag(s) ({hard} hard)  confidence {mean:.2f}"
        )
    if plan.verify:
        typer.echo(f"\nverification: {plan.verify.summary()}")
    typer.echo(json.dumps({"unchecked_sources": sorted(unchecked)}))


def register(app: typer.Typer) -> None:
    app.command("export")(export)
    app.command("summary")(summary)
