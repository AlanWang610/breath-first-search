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


def register(app: typer.Typer) -> None:
    app.command("load-osm")(load_osm)
    app.command("load-tiger")(load_tiger)
    app.command("load-nhd")(load_nhd)
    app.command("load-gtfs")(load_gtfs)
