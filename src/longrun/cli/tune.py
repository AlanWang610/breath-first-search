"""`longrun tune` - fit the six priority parameters, or say why it will not (scope 7.1).

Reads preference pairs from `tests/eval/pairs/`, or records a new one from a GPX a runner
edited, and runs the grid. Most of the time it will decline to fit, which is the command
working: both sources scope 7.1 names are empty on a fresh install and a vector fitted to
three pairs is noise that presents as a measurement (ADR 0021).

`docs/tuning.md` is the published version of whatever this prints.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from longrun.core.labels.pairs_io import dump_pairs, read_pairs
from longrun.core.routing.custom_model import describe
from longrun.core.routing.tuning import fit

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.labels.reroute import RerouteReport

#: Where a labelled set lives by default. Under `tests/` rather than in the package,
#: because a preference set is evidence about a person and belongs beside the eval cases
#: that read the same rule about authorship (ADR 0022).
DEFAULT_PAIRS_DIR = Path("tests/eval/pairs")


def _summarise_gpx(path: Path, name: str, root: Path, snapshot_path: Path | None) -> Any:
    """One GPX through the store seam, to metres per way class.

    Goes through `open_context` rather than reading the GeoPackage directly, because the
    way ids a route carries have to be the ones the scorers would have assigned it -
    `assign_way_ids` over the corridor, not a nearest-neighbour of somebody's own devising.
    A pair built on different matching from the plan's would be fitting parameters against
    a route this project never scored.
    """
    from datetime import datetime

    from longrun.core.geo.gpx import gpx_read
    from longrun.core.geo.segments import corridor, segment_route
    from longrun.core.models.plan import SnapshotPins
    from longrun.core.plan.pipeline import match_ways
    from longrun.core.preferences.store import load_defaults
    from longrun.core.routing.preferences import summarise
    from longrun.core.scorers._common import frame_tags_by_way
    from longrun.runtime import open_context

    route = gpx_read(path)
    snapshot = (
        SnapshotPins.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        if snapshot_path
        else SnapshotPins()
    )
    with open_context(
        route=route,
        root=root,
        snapshot=snapshot,
        start_at=datetime(2026, 1, 1, 8, 0),
        profile=load_defaults(),
        offline=True,
    ) as ctx:
        match = match_ways(route, ctx)
        way_ids = None if match is None else list(match.way_ids)
        segments = segment_route(route, way_ids)
        try:
            frame = ctx.layers.ways_in_corridor(corridor(route))
            tags = frame_tags_by_way(frame)
        except Exception:  # noqa: BLE001 - no ways layer is a summary of unknowns, not a crash
            tags = {}
    return summarise(name, segments, tags)


def tune(
    pairs_dir: Path = typer.Option(
        DEFAULT_PAIRS_DIR, "--pairs", help="Directory of preference-pair YAML files."
    ),
    profile_path: Path | None = typer.Option(
        None, "--profile", help="Preference profile, for the detour tolerance."
    ),
    edit: tuple[Path, Path] = typer.Option(
        (None, None),
        "--edit",
        help="Record a pair from a route you edited: ORIGINAL.gpx EDITED.gpx.",
    ),
    fixtures: Path = typer.Option(Path("data"), "--fixtures", help="Layer store root, for --edit."),
    snapshot_path: Path | None = typer.Option(None, "--snapshot", help="Snapshot pins."),
    labeler: str | None = typer.Option(
        None, "--labeler", help="Fit one runner's pairs only (ADR 0021: never globally)."
    ),
    holdout: int = typer.Option(
        0, "--holdout", help="K-fold held-out agreement, against neutral; 0 skips it."
    ),
    save: bool = typer.Option(
        False,
        "--save",
        help="Make this fit the vector your plans are drawn with, if every ADR 0051 gate passes.",
    ),
    reroute_check: bool = typer.Option(
        False,
        "--reroute",
        help="Re-draw each pair once with the fit and report (ADR 0020's check); --save runs it.",
    ),
    router_url: str | None = typer.Option(
        None, "--router", help="GraphHopper URL for the re-route check; default by region."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Fit the six scope 7.1 priority parameters against recorded preference pairs.

    A person's own answers are in `~/.longrun/labels/pairs` (`longrun label`, ADR 0049):
    `longrun tune --pairs ~/.longrun/labels/pairs --labeler NAME --holdout 5`.

    `--save` writes the fit into your profile's `routing` entry, and `longrun plan` draws
    with it from then on (ADR 0051). It refuses unless the fit beats neutral held out, buys
    no detours, and the router re-drawing each pair lands on your choice more often than
    not - and it is yours to run: adopting a fit is a decision about your routes.
    """
    from longrun.core.labels.adopt import fitted_entry, refusals
    from longrun.core.preferences.store import ProfileError, load_profile, save_entry
    from longrun.core.routing.preferences import pairs_from_edit
    from longrun.core.routing.tuning import evidence_by_parameter, holdout_agreement

    if edit and edit[0] is not None and edit[1] is not None:
        original, edited = edit
        for candidate in (original, edited):
            if not candidate.is_file():
                typer.echo(f"error: no such GPX: {candidate}", err=True)
                raise typer.Exit(code=2)
        recorded = pairs_from_edit(
            _summarise_gpx(original, original.stem, fixtures, snapshot_path),
            _summarise_gpx(edited, edited.stem, fixtures, snapshot_path),
        )
        if not recorded:
            typer.echo("the two routes use the same ways; nothing was preferred, so no pair")
            raise typer.Exit(code=0)
        pairs_dir.mkdir(parents=True, exist_ok=True)
        out = pairs_dir / f"{edited.stem}.yaml"
        out.write_text(dump_pairs(recorded), encoding="utf-8")
        typer.echo(f"recorded {len(recorded)} pair(s) to {out}")

    pairs = read_pairs(pairs_dir)
    if labeler is not None:
        # A pair that does not say whose it is belongs to nobody in particular, and a fit
        # "for the runner it was fitted for" (ADR 0021) cannot use it.
        pairs = [pair for pair in pairs if pair.labeler == labeler]
    profile = load_profile(profile_path)
    result = fit(pairs, profile=profile)
    real = [pair for pair in pairs if pair.source != "synthetic"]
    evidence = evidence_by_parameter(real)
    # `--save` needs the held-out line whether or not `--holdout` asked for it: gate 5.
    folds = holdout if holdout > 0 else (5 if save else 0)
    held = (
        holdout_agreement(real, folds=folds, profile=profile)
        if folds > 0 and len(real) >= 2
        else None
    )
    # Gates 1-5 first, so a fit that fails one never spends a routing call on gate 6.
    problems = refusals(result, labeler=labeler, holdout=held) if save else []
    report = (
        _reroute(real, result.params, profile, router_url)
        if real and not result.params.is_neutral and (reroute_check or (save and not problems))
        else None
    )
    if save and report is not None:
        problems.extend(report.problems())
    elif save and not problems:
        problems.append("the re-route check did not run")
    saved: Path | None = None
    if save and not problems:
        assert labeler is not None and held is not None and report is not None
        today = date.today()
        try:
            saved = save_entry(
                "routing",
                fitted_entry(result, labeler=labeler, holdout=held, report=report, today=today),
                profile_path,
                comment=(
                    f"Scope 7.1's routing vector, fitted to {labeler}'s answers and saved by\n"
                    f"`longrun tune --save` on {today:%Y-%m-%d} (ADR 0051). `longrun plan` "
                    "draws with it;\n`longrun plan --neutral` draws without. Delete this block "
                    "to go back to neutral."
                ),
            )
        except ProfileError as exc:
            problems.append(str(exc))

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "params": result.params.model_dump(),
                    "agreement": round(result.agreement, 4),
                    "penalty": round(result.penalty, 4),
                    "pairs_by_source": result.pairs_by_source,
                    "real_pairs": result.real_pairs,
                    "grid_size": result.grid_size,
                    "caveats": result.caveats,
                    "labeler": labeler,
                    "evidence_m": evidence,
                    "holdout": None
                    if held is None
                    else {
                        "folds": held.folds,
                        "pairs": held.pairs,
                        "fitted": round(held.fitted, 4),
                        "neutral": round(held.neutral, 4),
                        "avoid_high_stress": round(held.avoid_high_stress, 4),
                        "interval": [round(v, 4) for v in held.interval],
                        "beats_neutral": held.beats_neutral,
                    },
                    "reroute": None
                    if report is None
                    else {
                        "rerouted": len(report.rows),
                        "unchecked": report.unchecked,
                        "chosen": report.count("chosen"),
                        "rejected": report.count("rejected"),
                        "neither": report.count("neither"),
                        "refused": report.count("refused"),
                        "over_tolerance": len(report.over_tolerance),
                        "passed": report.passed,
                    },
                    "saved": None if saved is None else str(saved),
                    "not_saved_because": problems,
                },
                indent=2,
            )
        )
    else:
        typer.echo(str(result))
        for caveat in result.caveats:
            typer.echo(f"  {caveat}")
        if real:
            # A parameter no pair ever separated two lines on has a fitted value that is the
            # grid's tie-break talking, not the runner.
            typer.echo(
                "  evidence, metres of difference each parameter has seen: "
                + ", ".join(f"{name} {metres:.0f}" for name, metres in evidence.items())
            )
        if held is not None:
            typer.echo(f"  {held}")
        elif folds > 0:
            typer.echo("  too few pairs from a person to hold any out")
        if report is not None:
            typer.echo(f"  {report}")
        elif reroute_check:
            typer.echo("  nothing to re-draw: the fit is neutral, which is what plans already use")
        if not pairs:
            typer.echo(f"  no pairs found in {pairs_dir}; see docs/tuning.md for how to record one")
        if saved is not None:
            typer.echo(
                f"saved to {saved}: `longrun plan` now draws with {describe(result.params)}; "
                "`--neutral` draws without it"
            )
        for problem in problems:
            typer.echo(f"  not saved: {problem}", err=True)
    if problems:
        raise typer.Exit(code=1)


def _reroute(pairs: list[Any], params: Any, profile: Any, router_url: str | None) -> RerouteReport:
    """Gate 6: each pair's ends from the ledger, routed once with the fitted vector.

    Built like `label seed`'s routers: resolved per task's region, each checked for a
    health answer once, so a stopped container is "start the router", not 50 refusals.
    """
    import httpx

    from longrun.core.labels.ledger import Ledger, labels_dir_from_env
    from longrun.core.labels.reroute import reroute
    from longrun.core.models.geometry import LatLon
    from longrun.core.routing.base import RouterUnavailable
    from longrun.core.routing.custom_model import to_custom_model
    from longrun.core.routing.graphhopper import GraphHopperRouter
    from longrun.regions.routers import resolve

    routers: dict[str, GraphHopperRouter] = {}

    def router_for(task: Any) -> GraphHopperRouter:
        ends = [LatLon(lat=lat, lon=lon) for lat, lon in task.ends]
        resolved = resolve(ends, region=task.region, explicit=router_url)
        if resolved.url not in routers:
            try:
                httpx.get(f"{resolved.url}/health", timeout=5.0).raise_for_status()
            except httpx.HTTPError as exc:
                raise RouterUnavailable(
                    f"no router answering at {resolved.url} ({exc}); start it with "
                    f"`./deploy/graphhopper/run.ps1 -Region {resolved.region or 'bayarea'}`"
                ) from None
            routers[resolved.url] = GraphHopperRouter(resolved.url)
        return routers[resolved.url]

    try:
        return reroute(
            pairs,
            Ledger(labels_dir_from_env()).tasks(),
            router_for,
            to_custom_model(params, profile) or None,
            tolerance_pct=float(profile.detour_tolerance_pct.value),
        )
    except RouterUnavailable as exc:
        typer.echo(f"error: the re-route check needs the router: {exc}", err=True)
        raise typer.Exit(code=3) from exc


def register(app: typer.Typer) -> None:
    app.command("tune")(tune)


__all__ = ["tune", "register"]
