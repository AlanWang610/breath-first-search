"""Region build commands (scope 10.1, 13).

The build is five idempotent steps and this module grows one command per step as they
land. `load-osm` is step 1 and the one M2 left owed: six scorers report `unavailable` on
the `bay-urban` golden for want of a `ways` layer, `longrun freeze-fixture` is written and
tested and has nothing to freeze from.

Dev-only, and it says so by needing things CI does not have — the `ingest` extra, a 233 MB
extract and a live PostGIS:

    docker compose -f deploy/docker-compose.yml up -d
    uv run longrun load-osm data/osm/bayarea.osm.pbf --region bayarea
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from longrun.core.data.postgis import dsn_from_env


def load_osm(
    pbf: Path = typer.Argument(..., help="OSM extract to load, clipped to the region."),
    region: str = typer.Option(..., "--region", help="Region name for meta.layer_vintage."),
    dsn: str | None = typer.Option(None, "--dsn", help="PostGIS connection string."),
    schema: str = typer.Option("osm", "--schema", help="Schema to load into."),
    source_url: str | None = typer.Option(
        None, "--source-url", help="Where the extract came from, recorded with the vintage."
    ),
    truncate: bool = typer.Option(
        False, "--truncate", help="Empty the tables first, for a rebuild from scratch."
    ),
) -> None:
    """Load an OSM extract into `<schema>.{ways,railways,nodes,amenities}` (ADR 0009, 0010).

    Idempotent by upsert on the OSM id, so re-running it is a no-op and two regions that
    share a boundary way agree about it rather than collide. `--truncate` is the escape
    hatch for a rebuild, and it is not the default because a second region's load would
    then silently destroy the first.
    """
    if not pbf.exists():
        typer.echo(f"error: no such file: {pbf}", err=True)
        raise typer.Exit(code=2)

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - psycopg is a declared dependency
        typer.echo("error: psycopg is not installed", err=True)
        raise typer.Exit(code=2) from exc

    try:
        from longrun.core.data.osm import load_extract
    except ImportError as exc:
        typer.echo("error: the ingest extra is not installed: uv sync --extra ingest", err=True)
        raise typer.Exit(code=2) from exc

    target = dsn or dsn_from_env()
    try:
        connection = psycopg.connect(target, connect_timeout=10)
    except psycopg.OperationalError as exc:
        typer.echo(f"error: could not connect to {target}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    size_mb = pbf.stat().st_size / 1e6
    typer.echo(f"loading {pbf.name} ({size_mb:.0f} MB) into {schema} as region {region!r}")

    with connection:
        report = load_extract(
            pbf,
            connection,
            region=region,
            schema=schema,
            source_url=source_url,
            truncate=truncate,
        )

    for table, count in report.counts.items():
        typer.echo(f"  {schema}.{table}: {count:,} feature(s)")
    typer.echo(
        f"loaded {report.total:,} feature(s) in {report.elapsed_s:.0f}s, vintage {report.vintage}"
    )
    if report.total == 0:
        typer.echo("error: the extract produced no features", err=True)
        raise typer.Exit(code=1)


def _connect(dsn: str | None) -> Any:
    """Open the build's database connection, or exit saying why it could not."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - psycopg is a declared dependency
        typer.echo("error: psycopg is not installed", err=True)
        raise typer.Exit(code=2) from exc

    target = dsn or dsn_from_env()
    try:
        return psycopg.connect(target, connect_timeout=10)
    except psycopg.OperationalError as exc:
        typer.echo(f"error: could not connect to {target}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def load_tiger(
    region: str = typer.Option(..., "--region", help="Region name for meta.layer_vintage."),
    states: str = typer.Option(
        ..., "--states", help="Comma-separated two-digit state FIPS codes, e.g. 06,32."
    ),
    dsn: str | None = typer.Option(None, "--dsn", help="PostGIS connection string."),
    into: Path | None = typer.Option(None, "--into", help="Where downloads are cached."),
) -> None:
    """Load Census boundaries into `tiger.boundaries` (scope 13 step 4).

    States are required rather than defaulted: places are published per state, so a build
    has to know which states its polygon touches before it can ask for them. A region on a
    state line — scope 11 names one deliberately — passes both.
    """
    from longrun.core.data.national import load_tiger as run

    codes = [s.strip() for s in states.split(",") if s.strip()]
    if not codes:
        typer.echo("error: --states needs at least one FIPS code", err=True)
        raise typer.Exit(code=2)

    with _connect(dsn) as connection:
        counts = run(connection, region, codes, into=into)
    for what, count in counts.items():
        typer.echo(f"  {what}: {count:,} boundary(ies)")


def load_nhd(
    region: str = typer.Option(..., "--region", help="Region name for meta.layer_vintage."),
    huc4: str = typer.Option(..., "--huc4", help="Comma-separated HUC4 watershed codes."),
    dsn: str | None = typer.Option(None, "--dsn", help="PostGIS connection string."),
    into: Path | None = typer.Option(None, "--into", help="Where downloads are cached."),
) -> None:
    """Load NHD flowlines into `nhd.flowlines` (scope 7.6 water crossings).

    Per watershed, not per state: California's NHD download is 1,831 MB and the San
    Francisco Bay HUC4 is 121 MB for the same water a Bay Area region can reach.
    """
    from longrun.core.data.national import load_nhd as run

    codes = [s.strip() for s in huc4.split(",") if s.strip()]
    if not codes:
        typer.echo("error: --huc4 needs at least one watershed code", err=True)
        raise typer.Exit(code=2)

    with _connect(dsn) as connection:
        counts = run(connection, region, codes, into=into)
    for code, count in counts.items():
        typer.echo(f"  HUC4 {code}: {count:,} flowline(s)")


def load_gtfs(
    region: str = typer.Option(..., "--region", help="Region name for meta.layer_vintage."),
    feeds: str = typer.Option(
        ..., "--feeds", help="Comma-separated `id=path.zip` pairs, e.g. bart=data/gtfs/bart.zip."
    ),
    dsn: str | None = typer.Option(None, "--dsn", help="PostGIS connection string."),
) -> None:
    """Summarise GTFS feeds into `gtfs.stops` (scope 7.7).

    One row per boardable stop carrying what serves it and when, not a timetable — every
    `LayerStore` method is a corridor query, so a departures table would be unreachable by
    any scorer. See `core/data/gtfs.py`.

    Feeds are named rather than discovered: scope 13 asks for "all intersecting GTFS
    feeds", and discovery needs a registry with its own key and its own licence.
    """
    from longrun.core.data.gtfs import load_gtfs as run

    parsed: dict[str, Path] = {}
    for item in feeds.split(","):
        feed_id, _, location = item.strip().partition("=")
        if not feed_id or not location:
            typer.echo(f"error: expected id=path, got {item!r}", err=True)
            raise typer.Exit(code=2)
        path = Path(location)
        if not path.exists():
            typer.echo(f"error: no such feed: {path}", err=True)
            raise typer.Exit(code=2)
        parsed[feed_id] = path

    with _connect(dsn) as connection:
        counts = run(connection, region, parsed)
    for feed_id, count in counts.items():
        typer.echo(f"  {feed_id}: {count:,} stop(s)")
    if not any(counts.values()):
        typer.echo("error: no stop had a readable timetable", err=True)
        raise typer.Exit(code=1)


def build_region(
    spec_path: Path = typer.Argument(
        ..., help="Region spec YAML, e.g. deploy/regions/bayarea.yaml."
    ),
    dsn: str | None = typer.Option(None, "--dsn", help="PostGIS connection string."),
    manifest: Path | None = typer.Option(
        None, "--manifest", help="Where the build manifest is written. Defaults beside the spec."
    ),
    force: bool = typer.Option(False, "--force", help="Re-run steps a previous build finished."),
    only: str | None = typer.Option(
        None, "--only", help="Comma-separated step names to run, e.g. layers,coverage_report."
    ),
) -> None:
    """Build a region: the five idempotent steps of scope 13.

    Resumable. A run that dies in step 2 picks up in step 2 rather than re-fetching a
    hundred megabytes of TIGER to get there, and a step whose input does not exist is
    recorded as `blocked` with the reason rather than skipped silently.
    """
    from longrun.regions.build import RegionSpec
    from longrun.regions.build import build_region as run

    if not spec_path.exists():
        typer.echo(f"error: no such region spec: {spec_path}", err=True)
        raise typer.Exit(code=2)

    try:
        spec = RegionSpec.load(spec_path)
    except (ValueError, OSError) as exc:
        typer.echo(f"error: could not read {spec_path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    target = manifest or spec_path.with_suffix(".build.json")
    steps = [s.strip() for s in only.split(",")] if only else None
    typer.echo(f"building region {spec.name!r} from {spec_path}")

    with _connect(dsn) as connection:
        report = run(spec, connection, target, force=force, only=steps, log=typer.echo)

    typer.echo(f"manifest: {target}")
    failed = [name for name, record in report.steps.items() if record.status == "failed"]
    if failed:
        typer.echo(f"error: {', '.join(failed)} failed", err=True)
        raise typer.Exit(code=1)
    if not report.complete:
        typer.echo("incomplete: some steps have not run")


def promote_adapter(
    jurisdiction_id: str = typer.Argument(..., help="tiger:county:29095 or padus:NPS."),
    name: str = typer.Option(..., "--name", help="What to call it in the draft."),
    url: str = typer.Option(..., "--url", help="Where the jurisdiction publishes."),
    kind: str = typer.Option("closures", "--kind", help="closures|trail_status|access_hours."),
    out: Path | None = typer.Option(None, "--out", help="Where to write the module."),
    notes: str | None = typer.Option(None, "--notes", help="What the extraction found."),
) -> None:
    """Draft an adapter for a jurisdiction, for a human to finish (scope 7.10).

    Writes a module that does **not** work and says so, and prints the `pyproject.toml` line
    somebody has to paste. Nothing is registered: automatic registration would let a guess
    about a URL become a source of record, which inverts the point of tiers.
    """
    from longrun.adapters.base import valid_jurisdiction_id
    from longrun.adapters.promote import draft_adapter, drafts_parse
    from longrun.core.models.jurisdiction import Jurisdiction

    if not valid_jurisdiction_id(jurisdiction_id):
        typer.echo(
            f"error: {jurisdiction_id!r} is not a jurisdiction id. Expected "
            f"tiger:{{state|county|place}}:{{geoid}} or padus:{{CODE}}[:{{statefp}}].",
            err=True,
        )
        raise typer.Exit(code=2)
    if kind not in ("closures", "trail_status", "access_hours", "speed_survey"):
        typer.echo(f"error: unknown kind {kind!r}", err=True)
        raise typer.Exit(code=2)

    level = "park" if jurisdiction_id.startswith("padus:") else jurisdiction_id.split(":")[1]
    draft = draft_adapter(
        Jurisdiction(
            id=jurisdiction_id,
            level=level,  # type: ignore[arg-type]
            name=name,
            source="padus" if jurisdiction_id.startswith("padus:") else "tiger",
        ),
        kind,  # type: ignore[arg-type]
        url,
        notes=notes,
    )
    if not drafts_parse(draft):
        typer.echo("error: the draft did not parse; this is a bug in promote.py", err=True)
        raise typer.Exit(code=1)

    destination = out or Path("src") / Path(*draft.module_name.split(".")).with_suffix(".py")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(draft.source, encoding="utf-8")
    typer.echo(f"wrote {destination}")
    typer.echo("")
    typer.echo("Not registered. Review it, fill in fetch(), then add to pyproject.toml:")
    typer.echo('  [project.entry-points."longrun.adapters"]')
    typer.echo(f"  {draft.registration}")


def register(app: typer.Typer) -> None:
    app.command("load-osm")(load_osm)
    app.command("load-tiger")(load_tiger)
    app.command("load-nhd")(load_nhd)
    app.command("load-gtfs")(load_gtfs)
    app.command("build-region")(build_region)
    app.command("promote-adapter")(promote_adapter)
