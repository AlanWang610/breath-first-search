"""`longrun label` - where a person says what they think of a route (ADR 0048).

    longrun label show --golden boston-winter          # score it, write the sheet
    longrun label verdict --golden boston-winter --would-run no \\
        --reason "..." --bound fraction_lts3_plus_max=0.02
    longrun label publish --dry-run                    # what would enter tests/eval
    longrun label status

**This is a person's door.** An agent does not run `verdict` or `publish` (`CLAUDE.md`), and
`tools/` has no equivalent (`test_the_agents_door_has_no_way_to_record_a_label`). Labels go
to the ledger in `~/.longrun/labels/` first. `publish` is the only thing that writes them
into the tree, and the person reviews its diff.

`show` comes before `verdict` on purpose: a verdict about a golden is recorded against the
plan `show` last wrote, so it is always about a plan somebody was shown. Refusing to record
one otherwise is cheaper than finding out later that a label describes a route nobody opened.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
import yaml

from longrun.core.labels.bounds import (
    METRICS,
    check_verdict,
    measured_from_digest,
    measured_from_plan,
)
from longrun.core.labels.ledger import (
    LABELER_ENV_VAR,
    Ledger,
    labeler_from_env,
    labels_dir_from_env,
)
from longrun.core.labels.publish import case_yaml, differences, latest_by_golden
from longrun.core.labels.records import Verdict
from longrun.goldens import DEFAULT_GOLDENS_DIR, golden_args, is_golden

#: Where published verdicts go, relative to the repository root.
DEFAULT_CASES_DIR = Path("tests/eval/cases")

label_app = typer.Typer(
    help="Record what you think of a route: verdicts and preferences (ADR 0048).",
    no_args_is_help=True,
)


def _ledger() -> Ledger:
    return Ledger(labels_dir_from_env())


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _golden_dir(goldens: Path, name: str) -> Path:
    """A golden by name, never by path: a name that reaches outside the directory is refused,
    for the reason ADR 0036 gives for plan ids."""
    if not name or name != Path(name).name or name.startswith("."):
        raise _fail(f"{name!r} is not a golden's name")
    directory = goldens / name
    if not is_golden(directory):
        known = (
            sorted(d.name for d in goldens.iterdir() if is_golden(d)) if goldens.is_dir() else []
        )
        raise _fail(
            f"no golden named {name!r} under {goldens}; known: {', '.join(known) or 'none'}"
        )
    return directory


def _shown_dir(ledger: Ledger, golden: str) -> Path:
    return ledger.root / "shown" / golden


def _format(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _print_measured(measured: dict[str, Any]) -> None:
    typer.echo("What a bound can name, and this route's value:")
    width = max(len(metric) for metric in METRICS)
    for metric, meaning in METRICS.items():
        value = measured.get(metric)
        note = "  (not measured here: a bound on it is refused)" if value is None else ""
        typer.echo(f"  {metric:<{width}}  {_format(value):>10}  {meaning}{note}")


@label_app.command("show")
def show(
    golden: str = typer.Option(..., "--golden", help="The golden route to judge, by name."),
    goldens: Path = typer.Option(DEFAULT_GOLDENS_DIR, "--goldens", help="Golden routes."),
) -> None:
    """Score a golden the way its test does, and write the sheet to read before judging."""
    from typer.testing import CliRunner

    from longrun.cli.main import app
    from longrun.core.export.sheet_html import render_html
    from longrun.core.models.plan import Plan

    directory = _golden_dir(goldens, golden)
    out = _shown_dir(_ledger(), golden)
    run_dir = out / "run"

    # Through the CLI and with the golden's own argv, exactly as `tests/golden` runs it: the
    # verdict is re-checked against `expected.json`, so this has to be the plan that file
    # was written from, not one assembled by a second route to the same scorers.
    args = golden_args(directory, run_dir)
    typer.echo(f"scoring {golden} (`longrun {args[0]}`, offline, pinned inputs)...")
    result = CliRunner().invoke(app, args, env={"LONGRUN_OFFLINE": "1"})
    if result.exit_code != 0:
        raise _fail(f"`longrun {args[0]}` exited {result.exit_code}:\n{result.output}")

    plan_json = (run_dir / "plan.json").read_text(encoding="utf-8")
    plan = Plan.model_validate_json(plan_json)
    (out / "plan.json").write_text(plan_json, encoding="utf-8")
    sheet = out / "sheet.html"
    sheet.write_text(render_html(plan), encoding="utf-8")

    typer.echo(f"  sheet: {sheet}")
    typer.echo(f"  plan:  {out / 'plan.json'}")
    typer.echo("")
    _print_measured(measured_from_plan(plan))
    typer.echo("")
    typer.echo("Read the sheet, then say what you think:")
    typer.echo(
        f"  longrun label verdict --golden {golden} --would-run yes|no|unsure "
        '--reason "..." [--bound METRIC_max=VALUE ...] [--gap "..."]'
    )


def _bounds(entries: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for entry in entries:
        name, sep, text = entry.partition("=")
        if not sep:
            raise _fail(f"--bound {entry!r}: write it as METRIC_max=VALUE or METRIC_min=VALUE")
        try:
            out[name.strip()] = float(text)
        except ValueError:
            raise _fail(f"--bound {entry!r}: {text!r} is not a number") from None
    return out


def _current_case(cases: Path, golden: str) -> dict[str, Any] | None:
    path = cases / golden / "label.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


@label_app.command("verdict")
def verdict(
    golden: str | None = typer.Option(None, "--golden", help="A golden you ran `show` on."),
    plan_path: Path | None = typer.Option(
        None, "--plan", help="A stored plan.json instead; stays in the ledger (no golden)."
    ),
    would_run: str = typer.Option(..., "--would-run", help="yes, no or unsure."),
    reason: str = typer.Option(..., "--reason", help="Why, in a sentence of 12+ words."),
    bound: list[str] = typer.Option(  # noqa: B008 - Typer's idiom for a repeatable option
        [], "--bound", help="METRIC_max=VALUE or METRIC_min=VALUE: the envelope you'd accept."
    ),
    gap: str | None = typer.Option(None, "--gap", help="For a 'no' no number captures: what."),
    note: str | None = typer.Option(None, "--note", help="Anything else worth keeping."),
    cases: Path = typer.Option(DEFAULT_CASES_DIR, "--cases", help="tests/eval cases."),
) -> None:
    """Record whether you would run a route, and the bounds that say why."""
    from longrun.core.models.plan import Plan

    labeler = labeler_from_env()
    if labeler is None:
        raise _fail(f"set {LABELER_ENV_VAR} to the name your labels go under")
    if (golden is None) == (plan_path is None):
        raise _fail("say which plan: --golden NAME (after `label show`) or --plan PATH")

    ledger = _ledger()
    if golden is not None:
        source = _shown_dir(ledger, golden) / "plan.json"
        if not source.is_file():
            raise _fail(
                f"{golden} has not been shown; run `longrun label show --golden {golden}` and "
                "read its sheet first"
            )
    else:
        assert plan_path is not None
        if not plan_path.is_file():
            raise _fail(f"no such plan: {plan_path}")
        source = plan_path

    plan_json = source.read_text(encoding="utf-8")
    plan = Plan.model_validate_json(plan_json)
    measured = measured_from_plan(plan)
    bounds = _bounds(bound)

    problems = check_verdict(
        would_run=would_run, reason=reason, bounds=bounds, gap=gap, measured=measured
    )
    if problems:
        for problem in problems:
            typer.echo(f"  - {problem}", err=True)
        raise _fail("not recorded; nothing was written")

    try:
        record = Verdict(
            subject=golden or plan.id,
            golden=golden,
            would_run=would_run,  # type: ignore[arg-type]  # checked by check_verdict
            reason=reason,
            bounds=bounds,
            gap=gap,
            note=note,
            measured=measured,
            plan_sha256=hashlib.sha256(plan_json.encode("utf-8")).hexdigest(),
            labeler=labeler,
            labelled_at=datetime.now(UTC).replace(microsecond=0),
            via="cli",
        )
    except ValueError as exc:
        raise _fail(str(exc)) from None
    ledger.record_verdict(record, plan_json)
    typer.echo(f"recorded verdict {record.id}: {record.subject} - {would_run} (by {labeler})")

    if golden is None:
        typer.echo("  this plan has no golden, so the verdict stays in the ledger (ADR 0048)")
        return
    current = _current_case(cases, golden)
    if current is None:
        typer.echo(f"  `longrun label publish` would add a new case, tests/eval/cases/{golden}")
    elif current.get("by") != "human":
        agree = "agrees" if current.get("would_run") == would_run else "disagrees"
        typer.echo(
            f"  tests/eval holds a {current.get('by')} label for {golden}: "
            f"would_run {current.get('would_run')!r}. Yours {agree}; publish replaces it."
        )
    else:
        typer.echo("  tests/eval already holds a human label for it; publish replaces it")


@label_app.command("publish")
def publish(
    cases: Path = typer.Option(DEFAULT_CASES_DIR, "--cases", help="tests/eval cases."),
    goldens: Path = typer.Option(DEFAULT_GOLDENS_DIR, "--goldens", help="Golden routes."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be written."),
) -> None:
    """Write your latest verdict on each golden into tests/eval, for you to review and commit."""
    import json

    verdicts = _ledger().verdicts()
    latest = latest_by_golden(verdicts)
    unpublishable = len({v.subject for v in verdicts if v.golden is None})

    written = 0
    for name, record in sorted(latest.items()):
        directory = goldens / name
        if not is_golden(directory):
            typer.echo(f"skipped {name}: no golden by that name any more")
            continue
        digest = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
        moved = differences(record.measured, measured_from_digest(digest))
        if moved:
            typer.echo(
                f"skipped {name}: you judged numbers the golden no longer has "
                f"({'; '.join(moved)}); run `label show` and judge it again"
            )
            continue
        text = case_yaml(record)
        target = cases / name / "label.yaml"
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            continue
        if dry_run:
            typer.echo(f"--- would write {target}")
            typer.echo(text)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            typer.echo(f"wrote {target}")
        written += 1

    verb = "would write" if dry_run else "wrote"
    typer.echo(f"{verb} {written} case(s); review with `git diff tests/eval` before committing")
    if unpublishable:
        typer.echo(
            f"{unpublishable} plan(s) with no golden keep their verdicts in the ledger (ADR 0048)"
        )


@label_app.command("status")
def status() -> None:
    """What is in your ledger, and what of it could be published."""
    ledger = _ledger()
    verdicts = ledger.verdicts()
    typer.echo(f"ledger: {ledger.root}")
    if not verdicts:
        typer.echo("no verdicts yet; start with `longrun label show --golden NAME`")
        return
    latest: dict[str, Verdict] = {}
    for record in verdicts:
        latest[record.subject] = record
    typer.echo(f"{len(verdicts)} verdict(s) on {len(latest)} route(s); the latest on each:")
    for subject, record in sorted(latest.items()):
        where = "golden, publishable" if record.golden else "no golden, ledger only"
        gap = " (gap)" if record.gap else ""
        typer.echo(
            f"  {subject:<24} {record.would_run:<6}{gap} {record.labelled_at.date()} "
            f"by {record.labeler} - {where}"
        )


def register(app: typer.Typer) -> None:
    app.add_typer(label_app, name="label")


__all__ = ["register"]
