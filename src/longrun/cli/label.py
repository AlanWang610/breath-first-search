"""`longrun label` - where a person says what they think of a route (ADR 0048).

    longrun label show --golden boston-winter          # score it, write the sheet
    longrun label verdict --golden boston-winter --would-run no \\
        --reason "..." --bound fraction_lts3_plus_max=0.02
    longrun label publish --dry-run                    # what would enter tests/eval
    longrun label status
    longrun label import answers.json                  # answers a hosted page saved

**This is a person's door.** An agent does not run `verdict` or `publish` (`CLAUDE.md`), and
`tools/` has no equivalent (`test_the_agents_door_has_no_way_to_record_a_label`). Labels go
to the ledger in `~/.longrun/labels/` first. `publish` is the only thing that writes them
into the tree, and the person reviews its diff.

`show` comes before `verdict` on purpose: a verdict about a golden is recorded against the
plan `show` last wrote, so it is always about a plan somebody was shown. Refusing to record
one otherwise is cheaper than finding out later that a label describes a route nobody opened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
import yaml

from longrun.core.labels.bounds import (
    METRICS,
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
from longrun.goldens import (
    DEFAULT_GOLDENS_DIR,
    find_golden,
    golden_args,
    golden_names,
    is_golden,
)

#: Where published verdicts and pairs go, relative to the repository root. The pairs
#: directory is `tune`'s default, so a published pair is one `longrun tune` reads unasked.
DEFAULT_CASES_DIR = Path("tests/eval/cases")
DEFAULT_PAIRS_DIR = Path("tests/eval/pairs")

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
    directory = find_golden(goldens, name)
    if directory is None:
        known = ", ".join(golden_names(goldens)) or "none"
        raise _fail(f"{name!r} is not a golden under {goldens}; known: {known}")
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
    golden: str | None = typer.Option(
        None, "--golden", help="The golden you ran `show` on, or the golden --plan was seeded from."
    ),
    plan_path: Path | None = typer.Option(
        None, "--plan", help="A stored plan.json. With no --golden it stays in the ledger."
    ),
    would_run: str = typer.Option(..., "--would-run", help="yes, no or unsure."),
    reason: str = typer.Option(..., "--reason", help="Why, in a sentence of 12+ words."),
    bound: list[str] = typer.Option(  # noqa: B008 - Typer's idiom for a repeatable option
        [], "--bound", help="METRIC_max=VALUE or METRIC_min=VALUE: the envelope you'd accept."
    ),
    gap: str | None = typer.Option(None, "--gap", help="For a 'no' no number captures: what."),
    note: str | None = typer.Option(None, "--note", help="Anything else worth keeping."),
    cases: Path = typer.Option(DEFAULT_CASES_DIR, "--cases", help="tests/eval cases."),
    goldens: Path = typer.Option(DEFAULT_GOLDENS_DIR, "--goldens", help="Golden routes."),
) -> None:
    """Record whether you would run a route, and the bounds that say why.

    `--golden NAME` alone judges the plan `label show` last wrote for it. `--plan PATH`
    judges a stored plan; add `--golden NAME` when that plan was seeded from a golden, and
    `publish` will check that its numbers are still the golden's before writing anything.
    """
    from longrun.core.labels.verdicts import VerdictRefused, record_verdict

    labeler = labeler_from_env()
    if labeler is None:
        raise _fail(f"set {LABELER_ENV_VAR} to the name your labels go under")
    if golden is None and plan_path is None:
        raise _fail("say which plan: --golden NAME (after `label show`) or --plan PATH")

    ledger = _ledger()
    if plan_path is not None:
        if not plan_path.is_file():
            raise _fail(f"no such plan: {plan_path}")
        if golden is not None:
            _golden_dir(goldens, golden)
        source = plan_path
    else:
        assert golden is not None
        source = _shown_dir(ledger, golden) / "plan.json"
        if not source.is_file():
            raise _fail(
                f"{golden} has not been shown; run `longrun label show --golden {golden}` and "
                "read its sheet first"
            )

    try:
        record = record_verdict(
            ledger,
            source.read_text(encoding="utf-8"),
            golden=golden,
            would_run=would_run,
            reason=reason,
            bounds=_bounds(bound),
            gap=gap,
            note=note,
            labeler=labeler,
            labelled_at=datetime.now(UTC).replace(microsecond=0),
            via="cli",
        )
    except VerdictRefused as refused:
        for problem in refused.problems:
            typer.echo(f"  - {problem}", err=True)
        raise _fail("not recorded; nothing was written") from None
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
    pairs: Path = typer.Option(DEFAULT_PAIRS_DIR, "--pairs", help="tests/eval pairs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be written."),
) -> None:
    """Write your verdicts on goldens, and your pairs, into tests/eval to review and commit.

    A verdict goes in only when it names a golden whose numbers it was judged on. A pair goes
    in as two route summaries named by random candidate id - metres per way class, never a
    coordinate, never a place - because the repository is public and the lines are where you
    run (ADR 0048). "No preference", "can't judge" and asked-twice probes stay in the ledger.
    """
    import json

    ledger = _ledger()
    verdicts = ledger.verdicts()
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

    pairs_written = _publish_pairs(ledger, pairs, dry_run=dry_run)

    verb = "would write" if dry_run else "wrote"
    typer.echo(
        f"{verb} {written} case(s) and {pairs_written} pair(s); review with "
        "`git diff tests/eval` before committing"
    )
    if unpublishable:
        typer.echo(
            f"{unpublishable} plan(s) with no golden keep their verdicts in the ledger (ADR 0048)"
        )


def _publish_pairs(ledger: Ledger, directory: Path, *, dry_run: bool) -> int:
    """Each answered, non-probe choice as `judged-<labeler>-<task>.yaml`, rebuilt from the
    ledger's task and answer rather than copied, so what is published is exactly what
    `pairs.pair_from` says the answer is evidence for."""
    from longrun.core.labels.pairs import pair_from
    from longrun.core.labels.pairs_io import dump_pairs

    written = 0
    for answer in ledger.answers():
        task = ledger.task(answer.task_id)
        if task is None or task.repeat_of is not None:
            continue
        pair = pair_from(task, answer)
        if pair is None:
            continue
        text = dump_pairs([pair])
        target = directory / f"judged-{answer.labeler}-{task.id}.yaml"
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            continue
        if dry_run:
            typer.echo(f"--- would write {target}")
            typer.echo(text)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            typer.echo(f"wrote {target}")
        written += 1
    return written


@label_app.command("status")
def status() -> None:
    """What is in your ledger, and what of it could be published."""
    from longrun.core.labels.answers import probe_agreement

    ledger = _ledger()
    verdicts = ledger.verdicts()
    typer.echo(f"ledger: {ledger.root}")

    tasks, answers = ledger.tasks(), ledger.answers()
    counts = {
        kind: sum(1 for a in answers if a.prefer == kind) for kind in ("a", "b", "none", "cant")
    }
    agreeing, probed = probe_agreement(tasks, answers)
    typer.echo(
        f"pairs: {len(tasks)} queued, {len(answers)} answered - {counts['a'] + counts['b']} "
        f"chose a line, {counts['none']} no preference, {counts['cant']} could not judge; "
        f"probes {agreeing} of {probed} agree"
    )
    if answers:
        # docs/tuning.md: in dense grids the parameters barely move the line. A high rate of
        # "no preference" on pairs that were distinct by construction is evidence of that.
        typer.echo(f"  no-preference rate {counts['none'] / len(answers):.0%} of answered pairs")

    if not verdicts:
        typer.echo("verdicts: none yet; start with `longrun label show --golden NAME`")
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


# --- preference pairs (ADR 0049) -----------------------------------------------------


def _new_id() -> str:
    """Random, so nothing about a task's id - or which line is A - follows from how it was
    drawn. Twelve hex characters: a file name in three directories and a URL segment."""
    import secrets

    return secrets.token_hex(6)


def _parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        west, south, east, north = (float(part) for part in text.split(","))
    except ValueError:
        raise _fail(f"--bbox {text!r}: write it as WEST,SOUTH,EAST,NORTH") from None
    if not (west < east and south < north):
        raise _fail(f"--bbox {text!r}: west must be below east and south below north")
    return west, south, east, north


def _parse_km(text: str) -> tuple[float, float]:
    low, sep, high = text.partition("-")
    try:
        bounds = (float(low), float(high) if sep else float(low))
    except ValueError:
        raise _fail(f"--km {text!r}: write it as MIN-MAX, e.g. 3-9") from None
    if not 0 < bounds[0] <= bounds[1]:
        raise _fail(f"--km {text!r}: need 0 < MIN <= MAX")
    return bounds


def _sampled_ends(
    rng: Any, bbox: tuple[float, float, float, float], km: tuple[float, float]
) -> tuple[Any, Any] | None:
    """Two ends inside the box, a sampled distance apart; `None` if the box will not hold one."""
    import math

    from longrun.core.models.geometry import LatLon

    west, south, east, north = bbox
    for _ in range(50):
        lat, lon = rng.uniform(south, north), rng.uniform(west, east)
        metres, bearing = rng.uniform(*km) * 1000.0, rng.uniform(0.0, 2 * math.pi)
        to_lat = lat + metres * math.cos(bearing) / 110_540.0
        to_lon = lon + metres * math.sin(bearing) / (111_320.0 * math.cos(math.radians(lat)))
        if south <= to_lat <= north and west <= to_lon <= east:
            return LatLon(lat=lat, lon=lon), LatLon(lat=to_lat, lon=to_lon)
    return None


def _requested_ends(path: Path) -> list[tuple[Any, Any]]:
    from longrun.core.models.geometry import LatLon

    def point(text: str) -> LatLon:
        lat, _, lon = text.partition(",")
        return LatLon(lat=float(lat), lon=float(lon))

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    try:
        return [(point(str(entry["from"])), point(str(entry["to"]))) for entry in data]
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail(f"{path}: each entry needs from: 'lat,lon' and to: 'lat,lon' ({exc})") from None


@label_app.command("seed")
def seed(
    bbox: str | None = typer.Option(None, "--bbox", help="WEST,SOUTH,EAST,NORTH to sample in."),
    sample: int = typer.Option(20, "--sample", help="How many pairs of ends to try."),
    km: str = typer.Option("3-9", "--km", help="Distance between the ends, MIN-MAX km."),
    seed_value: int = typer.Option(1, "--seed", help="Sampling seed, so a box is repeatable."),
    requests: Path | None = typer.Option(
        None, "--requests", help="YAML list of {from: 'lat,lon', to: 'lat,lon'} instead."
    ),
    repeat: int = typer.Option(2, "--repeat", help="Test-retest probes: tasks asked twice."),
    router: str | None = typer.Option(None, "--router", help="GraphHopper URL; default by region."),
    region: str | None = typer.Option(None, "--region", help="Region, when the ends cannot say."),
    per_request: int = typer.Option(2, "--per-request", help="Questions per pair of ends."),
) -> None:
    """Draw lines between ends you might run between, and queue the pairs worth asking.

    Asks the router, which must be running for the region (`./deploy/graphhopper/run.ps1
    -Region bayarea`). Records questions, never answers: what you prefer is `label pair`, or
    the Review tab.
    """
    import random

    import httpx

    from longrun.core.labels.pairs import probe
    from longrun.core.labels.seeding import seed_request
    from longrun.core.routing.graphhopper import GraphHopperRouter
    from longrun.regions.routers import resolve

    if (bbox is None) == (requests is None):
        raise _fail("say where: --bbox WEST,SOUTH,EAST,NORTH, or --requests FILE")
    if requests is not None:
        ends_list = _requested_ends(requests)
    else:
        assert bbox is not None
        box, span, rng = _parse_bbox(bbox), _parse_km(km), random.Random(seed_value)
        sampled = [_sampled_ends(rng, box, span) for _ in range(sample)]
        ends_list = [ends for ends in sampled if ends is not None]
        if not ends_list:
            raise _fail(f"no two points {km} km apart fit inside {bbox}")

    ledger = _ledger()
    now = datetime.now(UTC).replace(microsecond=0)
    checked: set[str] = set()
    made = []
    for index, ends in enumerate(ends_list, 1):
        resolved = resolve(list(ends), region=region, explicit=router)
        if resolved.url not in checked:
            try:
                httpx.get(f"{resolved.url}/health", timeout=5.0).raise_for_status()
            except httpx.HTTPError as exc:
                raise _fail(
                    f"no router answering at {resolved.url} ({exc}); start it with "
                    f"`./deploy/graphhopper/run.ps1 -Region {resolved.region or 'bayarea'}`"
                ) from None
            checked.add(resolved.url)
        outcome = seed_request(
            GraphHopperRouter(resolved.url),
            ends,
            region=resolved.region,
            new_id=_new_id,
            created_at=now,
            per_request=per_request,
        )
        for task in outcome.tasks:
            ledger.add_task(task)
        made.extend(outcome.tasks)
        detail = (
            "; ".join(outcome.notes)
            if not outcome.tasks
            else ", ".join(t.stratum for t in outcome.tasks)
        )
        typer.echo(
            f"  {index:>3}/{len(ends_list)} {resolved.region or '?':<8} {outcome.lines} lines, "
            f"{outcome.distinct_lines} distinct, {len(outcome.tasks)} question(s): {detail}"
        )

    probes = [probe(task, _new_id(), now) for task in made[: max(repeat, 0)]]
    for task in probes:
        ledger.add_task(task)

    strata: dict[str, int] = {}
    for task in made:
        strata[task.stratum] = strata.get(task.stratum, 0) + 1
    typer.echo(
        f"queued {len(made)} question(s) from {len(ends_list)} pair(s) of ends, plus "
        f"{len(probes)} probe(s); by what differs: "
        + (", ".join(f"{name} {count}" for name, count in sorted(strata.items())) or "nothing")
    )
    typer.echo("answer them in the web UI's Review tab, or with `longrun label pair TASK ...`")


@label_app.command("queue")
def queue() -> None:
    """The unanswered questions, in the order a session asks them."""
    from longrun.core.labels.pairs import order

    ledger = _ledger()
    answered = {answer.task_id for answer in ledger.answers()}
    pending = order(task for task in ledger.tasks() if task.id not in answered)
    typer.echo(f"{len(pending)} unanswered, {len(answered)} answered")
    for task in pending:
        probe_note = f" (probe of {task.repeat_of})" if task.repeat_of else ""
        typer.echo(
            f"  {task.id}  {task.region or '?':<8} {task.stratum:<16} "
            f"A {task.a.length_m / 1000:.2f} km / B {task.b.length_m / 1000:.2f} km{probe_note}"
        )


@label_app.command("pair")
def pair(
    task_id: str = typer.Argument(..., help="The task, from `label queue`."),
    prefer: str = typer.Option(..., "--prefer", help="a, b, none (no preference) or cant."),
    strength: str | None = typer.Option(None, "--strength", help="slight or clear, with a or b."),
    reason: str | None = typer.Option(None, "--reason", help="Why, if you want to say."),
) -> None:
    """Record which of a task's two lines you would rather run."""
    from longrun.core.labels.answers import AnswerRefused, record_pair_answer
    from longrun.core.labels.ledger import AlreadyAnswered

    labeler = labeler_from_env()
    if labeler is None:
        raise _fail(f"set {LABELER_ENV_VAR} to the name your labels go under")
    try:
        answer = record_pair_answer(
            _ledger(),
            task_id,
            prefer=prefer,
            strength=strength,
            reason=reason,
            labeler=labeler,
            labelled_at=datetime.now(UTC).replace(microsecond=0),
            via="cli",
        )
    except AnswerRefused as refused:
        for problem in refused.problems:
            typer.echo(f"  - {problem}", err=True)
        raise _fail("not recorded; nothing was written") from None
    except AlreadyAnswered as exc:
        raise _fail(f"{exc}; a first answer stands") from None
    counted = "a pair `tune` will read" if answer.prefer in ("a", "b") else "kept, not a pair"
    typer.echo(f"recorded {answer.prefer} on {task_id} (by {labeler}): {counted}")


# --- a batch answered on a hosted page (ADR 0050) ------------------------------------


@label_app.command("batch")
def batch(
    out: Path | None = typer.Option(
        None, "--out", help="Where the batch goes; default `batches/` beside the ledger."
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="At most this many questions, not counting ones asked twice."
    ),
    stratum: list[str] = typer.Option(  # noqa: B008 - Typer's idiom for a repeatable option
        [], "--stratum", help="Only questions about this parameter; repeatable."
    ),
) -> None:
    """Gather the ledger's unanswered questions into a batch a hosted page can ask.

    Writes BATCH.json (the tasks, kept on this machine) and BATCH.page.json (what the page
    may hold: no vector names, no probe flags). Answers come back with `label import`.
    `--stratum` aims a batch at the parameters earlier answers left unpinned.
    """
    import json

    from longrun.core.labels.batch import batches_dir, make_batch, page_payload
    from longrun.core.labels.pairs import order
    from longrun.core.routing.custom_model import PARAMETER_NAMES

    unknown = sorted(set(stratum) - set(PARAMETER_NAMES))
    if unknown:
        raise _fail(f"--stratum {', '.join(unknown)}: expected one of {', '.join(PARAMETER_NAMES)}")
    ledger = _ledger()
    answered = {answer.task_id for answer in ledger.answers()}
    pending = order(task for task in ledger.tasks() if task.id not in answered)
    if stratum:
        # A probe carries its original's stratum, so it is kept or dropped with it.
        pending = [task for task in pending if task.stratum in stratum]
    if limit is not None:
        # The first questions in session order are the ones worth asking first. A probe sorts
        # after all of them, so it is kept by its original rather than by its position.
        kept = set([task.id for task in pending if task.repeat_of is None][: max(limit, 0)])
        pending = [task for task in pending if (task.repeat_of or task.id) in kept]
    if not pending:
        raise _fail("no unanswered questions in the ledger; queue some with `label seed`")
    now = datetime.now(UTC).replace(microsecond=0)
    made = make_batch(f"{now:%Y%m%d}-{_new_id()[:6]}", pending, now)
    directory = out or batches_dir(ledger.root)
    directory.mkdir(parents=True, exist_ok=True)
    tasks_path = directory / f"{made.id}.json"
    page_path = directory / f"{made.id}.page.json"
    tasks_path.write_text(made.model_dump_json(indent=1) + "\n", encoding="utf-8")
    page_path.write_text(json.dumps(page_payload(made)) + "\n", encoding="utf-8")
    probes = sum(1 for task in made.tasks if task.repeat_of)
    typer.echo(f"batch {made.id}: {len(made.tasks)} question(s), {probes} of them asked twice")
    typer.echo(f"  tasks: {tasks_path}")
    typer.echo(f"  page:  {page_path}")


@label_app.command("import")
def import_(
    answers_path: Path = typer.Argument(..., help="The answers file the page saved."),
    tasks: Path | None = typer.Option(
        None, "--tasks", help="The batch's tasks; default `batches/BATCH.json` beside the ledger."
    ),
) -> None:
    """Record the answers a hosted page saved, as yours (`LONGRUN_LABELER`)."""
    from pydantic import ValidationError

    from longrun.core.labels.batch import AnswersFile, Batch, batches_dir, import_answers

    labeler = labeler_from_env()
    if labeler is None:
        raise _fail(f"set {LABELER_ENV_VAR} to the name your labels go under")
    try:
        answers = AnswersFile.model_validate_json(answers_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise _fail(f"{answers_path} is not an answers file the page saved ({exc})") from None
    ledger = _ledger()
    tasks_path = tasks or batches_dir(ledger.root) / f"{answers.batch}.json"
    try:
        made = Batch.model_validate_json(tasks_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise _fail(f"no batch {answers.batch} at {tasks_path}; pass --tasks ({exc})") from None

    from longrun.core.labels.answers import AnswerRefused

    try:
        report = import_answers(ledger, made, answers, labeler=labeler)
    except AnswerRefused as refused:
        raise _fail("; ".join(refused.problems)) from None
    chose = sum(1 for answer in report.recorded if answer.prefer in ("a", "b"))
    typer.echo(
        f"recorded {len(report.recorded)} answer(s) by {labeler} into {ledger.root}: "
        f"{chose} chose a line, {len(report.recorded) - chose} did not"
    )
    if report.already:
        typer.echo(f"  {len(report.already)} already in the ledger, unchanged")
    if report.kept_first:
        typer.echo(
            f"  {len(report.kept_first)} answered differently before; the first answer stands: "
            + ", ".join(report.kept_first)
        )
    for task_id, problems in report.refused:
        typer.echo(f"  not recorded {task_id}: {'; '.join(problems)}", err=True)
    if report.refused:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.add_typer(label_app, name="label")


__all__ = ["register"]
