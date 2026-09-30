"""`longrun tune` - fit the six priority parameters, or say why it will not (scope 7.1).

Reads preference pairs from `tests/eval/pairs/`, or records a new one from a GPX a runner
edited, and runs the grid. Most of the time it will decline to fit, which is the command
working: both sources scope 7.1 names are empty on a fresh install and a vector fitted to
three pairs is noise that presents as a measurement (ADR 0021).

`docs/tuning.md` is the published version of whatever this prints.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from longrun.core.labels.pairs_io import dump_pairs, read_pairs
from longrun.core.routing.tuning import fit

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
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Fit the six scope 7.1 priority parameters against recorded preference pairs."""
    from longrun.core.preferences.store import load_profile
    from longrun.core.routing.preferences import pairs_from_edit

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
    result = fit(pairs, profile=load_profile(profile_path))

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
                },
                indent=2,
            )
        )
        return

    typer.echo(str(result))
    for caveat in result.caveats:
        typer.echo(f"  {caveat}")
    if not pairs:
        typer.echo(f"  no pairs found in {pairs_dir}; see docs/tuning.md for how to record one")


def register(app: typer.Typer) -> None:
    app.command("tune")(tune)


__all__ = ["tune", "register"]
