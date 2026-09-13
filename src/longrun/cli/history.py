"""`longrun ingest-history` - run history becomes a pacing curve (scope 6.2).

Local-first and enforced rather than promised (scope 3.7): the files are read where they
sit, nothing is copied, only derived curves are written, and the first and last 500 m of
every activity are dropped before anything looks at where it was.

Writes nothing unless asked. A derivation printed and not saved is the useful default for
the first run, when what a person wants to know is whether the numbers look like them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer


def ingest_history(
    source: Path = typer.Argument(..., help="A .fit file, or a directory of them."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Preference profile YAML."),
    router_url: str | None = typer.Option(
        None, "--router", help="GraphHopper base URL, for the accepted-road set."
    ),
    save: bool = typer.Option(False, "--save", help="Write the derived curves to the profile."),
) -> None:
    """Derive a pacing curve from run history."""
    from longrun.core.models.profile import PreferenceEntry, Provenance
    from longrun.core.pacing.history import ingest, read_fit
    from longrun.core.preferences.store import load_profile, merge, save_profile

    files = sorted(source.glob("**/*.fit")) if source.is_dir() else [source]
    if not files:
        typer.echo(f"error: no .fit files under {source}", err=True)
        raise typer.Exit(code=2)

    activities = []
    unreadable = 0
    for path in files:
        try:
            activity = read_fit(path)
        except Exception as exc:  # noqa: BLE001 - one bad file is not the whole history
            typer.echo(f"  {path.name}: {type(exc).__name__}: {exc}")
            unreadable += 1
            continue
        if activity is None:
            typer.echo(f"  {path.name}: no track points")
            unreadable += 1
            continue
        activities.append(activity)

    router = None
    if router_url is not None:
        from longrun.core.routing.graphhopper import GraphHopperRouter

        router = GraphHopperRouter(router_url)

    history = ingest(activities, router=router)
    curves = history.curves

    typer.echo(f"read {history.activities_read} activity(ies), {unreadable} unreadable")
    typer.echo(f"provenance: {curves.provenance}")
    typer.echo(f"longest effort: {(curves.longest_effort_m or 0) / 1000:.1f} km")
    typer.echo(f"flat speed: {curves.flat_speed_ms:.3f} m/s")
    typer.echo(f"fatigue drift: {curves.fatigue_drift_pct_per_10km:.2f}% per 10 km")
    typer.echo(f"measured grade bins: {len(curves.speed_by_grade_bin)}")
    if history.accepted_ways:
        typer.echo(f"accepted roads: {len(history.accepted_ways)} way(s) run twice or more")
    for reason in history.reasons:
        typer.echo(f"  note: {reason}")

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


def _today() -> date:
    """Wall time, which `cli/` may read and `core/` may not."""
    return date.today()


def register(app: typer.Typer) -> None:
    app.command("ingest-history")(ingest_history)


__all__ = ["ingest_history", "register"]
