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


def register(app: typer.Typer) -> None:
    app.command("load-osm")(load_osm)
