"""`longrun ingest-history` - run history becomes a pacing curve (scope 6.2).

Local-first and enforced rather than promised (scope 3.7): the files are read where they
sit - a Strava export straight out of its zip - nothing is copied, only derived curves are
written, and the first and last 500 m of every activity are dropped before anything looks
at where it was.

`--remote-rasters` is the one exception to "nothing leaves the machine", and it is off
unless asked, as it is for `repair` and `plan`: it reads 3DEP over the network, so which
one-degree tiles a history covers, and which blocks of them, reach USGS's public bucket.
It is worth asking for, because a watch's altimeter understates hills and a plan grades
on terrain.

Writes nothing unless asked. A derivation printed and not saved is the useful default for
the first run, when what a person wants to know is whether the numbers look like them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.pacing.curves import PacingCurves
    from longrun.core.pacing.history import Activity, ElevationSource

#: The grade bins printed for a person to check against their own sense of their running.
SHOWN_BINS = range(-12, 14, 2)


def ingest_history(
    source: Path = typer.Argument(
        ...,
        help="A Strava export (.zip or its unzipped folder), a folder of .fit/.gpx files, "
        "or one file.",
    ),
    profile_path: Path | None = typer.Option(None, "--profile", help="Preference profile YAML."),
    router_url: str | None = typer.Option(
        None, "--router", help="GraphHopper base URL, for the accepted-road set."
    ),
    remote_rasters: bool = typer.Option(
        False,
        "--remote-rasters",
        help="Grade on 3DEP terrain read over the network, as plans do, not device elevation.",
    ),
    save: bool = typer.Option(False, "--save", help="Write the derived curves to the profile."),
) -> None:
    """Derive a pacing curve from run history."""
    from longrun.core.models.profile import PreferenceEntry, Provenance
    from longrun.core.pacing.history import ingest
    from longrun.core.preferences.store import load_profile, merge, save_profile

    if not source.exists():
        typer.echo(f"error: {source} does not exist", err=True)
        raise typer.Exit(code=2)

    activities, notes = _read(source)

    elevation: ElevationSource = "device"
    if remote_rasters:
        activities, uncovered, tiles = _on_terrain(activities)
        elevation = "terrain"
        typer.echo(
            f"terrain: 3DEP elevation from {tiles} tile(s); {uncovered} activity(ies) had "
            "none (outside the US, on a tile edge, or all inside the privacy trim)"
        )
    else:
        notes.append(
            "grades use the device's own elevation, which understates hills: pass "
            "--remote-rasters to grade on the 3DEP terrain a plan is scored on"
        )

    router = None
    if router_url is not None:
        from longrun.core.routing.graphhopper import GraphHopperRouter

        router = GraphHopperRouter(router_url)

    history = ingest(activities, router=router, elevation=elevation)
    curves = history.curves

    typer.echo(f"provenance: {curves.provenance}")
    typer.echo(f"longest effort: {(curves.longest_effort_m or 0) / 1000:.1f} km")
    typer.echo(f"flat speed: {curves.flat_speed_ms:.3f} m/s ({_pace(curves.flat_speed_ms)})")
    typer.echo(f"fatigue drift: {curves.fatigue_drift_pct_per_10km:.2f}% per 10 km")
    typer.echo(
        f"measured grade bins: {len(curves.speed_by_grade_bin)}"
        + (f" (on {curves.grade_elevation} elevation)" if curves.grade_elevation else "")
    )
    for line in _grade_table(curves):
        typer.echo(line)
    if history.accepted_ways:
        typer.echo(f"accepted roads: {len(history.accepted_ways)} way(s) run twice or more")
    for note in [*notes, *history.reasons]:
        typer.echo(f"  note: {note}")

    if not save:
        typer.echo("nothing written (pass --save to store these curves)")
        return

    profile = load_profile(profile_path)
    # INFERRED, never STATED: a curve is measured, and scope 6.3 keeps `stated` for what a
    # person actually said - so a runner who tells the profile otherwise still wins.
    incoming = profile.model_copy(
        update={
            "pacing": PreferenceEntry(
                value=curves, provenance=Provenance.INFERRED, updated=_today()
            )
        }
    )
    merged, refused = merge(profile, incoming)
    written = save_profile(merged, profile_path)
    typer.echo(f"wrote {written}")
    for key in refused:
        typer.echo(f"  refused: {key} is already stated and outranks an inferred value")


def _read(source: Path) -> tuple[list[Activity], list[str]]:
    """Activities from any of the three shapes a history arrives in, and what was skipped."""
    from longrun.core.pacing.history import READABLE_SUFFIXES, read_activity
    from longrun.core.pacing.strava import is_strava_export, read_export

    if is_strava_export(source):
        export = read_export(source)
        typer.echo(
            f"Strava export: {len(export.activities)} run(s) read of {export.listed} activities"
        )
        if export.not_runs:
            skipped = ", ".join(f"{kind} {count}" for kind, count in export.not_runs.items())
            typer.echo(f"  not runs, never opened: {skipped}")
        for entry in export.unreadable:
            typer.echo(f"  {entry}")
        return export.activities, list(export.notes)

    if source.is_dir():
        files = sorted(p for p in source.rglob("*") if p.name.lower().endswith(READABLE_SUFFIXES))
        if not files:
            typer.echo(f"error: no .fit or .gpx files under {source}", err=True)
            raise typer.Exit(code=2)
    else:
        files = [source]

    activities = []
    unreadable = 0
    for path in files:
        try:
            activity = read_activity(path.name, path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - one bad file is not the whole history
            typer.echo(f"  {path.name}: {type(exc).__name__}: {exc}")
            unreadable += 1
            continue
        if activity is None:
            typer.echo(f"  {path.name}: no track points")
            unreadable += 1
            continue
        activities.append(activity)
    typer.echo(f"read {len(activities)} activity(ies), {unreadable} unreadable")
    return activities, []


def _on_terrain(activities: list[Activity]) -> tuple[list[Activity], int, int]:
    """3DEP under every activity, one store per one-degree tile, the tile under its middle."""
    import tempfile

    from longrun.core.data.file_store import FileRasterStore
    from longrun.core.data.rasters import three_dep_url
    from longrun.core.geo.dem import sample_elevation
    from longrun.core.models.context import Budget
    from longrun.core.models.geometry import Route
    from longrun.core.pacing.history import with_terrain

    # One window per activity. The plan-sized default of 500 is a ceiling on one plan's
    # reads, and a history is not a plan.
    budget = Budget(raster_windows_max=max(1, len(activities)))
    stores: dict[str, FileRasterStore] = {}
    # An empty root, so a `dem.tif` in the working directory cannot stand in for 3DEP.
    with tempfile.TemporaryDirectory() as empty:

        def sample(route: Route) -> list[float | None]:
            middle = route.points[len(route.points) // 2]
            url = three_dep_url(middle.lat, middle.lon)
            if url not in stores:
                stores[url] = FileRasterStore(Path(empty), remote={"dem": url}, budget=budget)
            return sample_elevation(route, stores[url])

        on_terrain, uncovered = with_terrain(activities, sample)
    return on_terrain, uncovered, len(stores)


def _grade_table(curves: PacingCurves) -> list[str]:
    lines = []
    for lower in SHOWN_BINS:
        speed = curves.speed_by_grade_bin.get(f"{lower:+d}")
        if speed is None:
            continue
        lines.append(
            f"  {lower:+3d} to {lower + 2:+3d}%  {_pace(speed)}  "
            f"({speed / curves.flat_speed_ms:.2f}x flat)"
        )
    return lines


def _pace(speed_ms: float) -> str:
    seconds = round(1000 / speed_ms)
    return f"{seconds // 60}:{seconds % 60:02d}/km"


def _today() -> date:
    """Wall time, which `cli/` may read and `core/` may not."""
    return date.today()


def register(app: typer.Typer) -> None:
    app.command("ingest-history")(ingest_history)


__all__ = ["ingest_history", "register"]
