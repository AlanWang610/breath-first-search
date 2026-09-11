"""`longrun freeze-fixture` — turn a live corridor into committed test fixtures (scope 11).

The mechanism the whole test strategy rests on. Most scorers need PostGIS for ground
truth, but the test suite must not touch a database and CI has no service containers. The
resolution is to run the corridor queries **once**, against the real thing, and commit
what came back: a golden route's GeoPackage is then literally the answer PostGIS gave,
rather than a hand-drawn approximation of it that drifts from the production path.

That is only true because the freeze goes through `PostGISLayerStore` rather than issuing
its own SQL. The same corridor geometry, the same predicate, the same tag flattening as a
scorer would get — so anything the fixture does not contain is something a scorer would
not have seen either. A freeze with a private query would be a second implementation, and
a fixture that agreed with nothing.

`network`-marked wherever it is exercised, and never run in CI.

    docker compose -f deploy/docker-compose.yml up -d
    uv run longrun freeze-fixture route.gpx --out tests/golden/routes/bay-urban/fixtures
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from longrun.core.data.air import route_air_quality
from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore, _resolution_m
from longrun.core.data.forecast import DEFAULT_SPACING_M, route_forecast
from longrun.core.data.postgis import DEFAULT_LAYER_TABLES, PostGISLayerStore, dsn_from_env
from longrun.core.geo.gpx import GpxError, gpx_read
from longrun.core.geo.segments import corridor
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.plan import SnapshotPins
from longrun.core.models.profile import PreferenceProfile

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

    from longrun.core.models.geometry import Corridor

#: Layers whose features are points, and so are fetched with the point query. Everything
#: else goes through `polygons_intersecting`, which in `PostGISLayerStore` is the same
#: `ST_Intersects` against the same corridor polygon — the three method names on the
#: protocol describe what a *scorer* is asking, not three different predicates.
POINT_LAYERS = frozenset({"nodes", "amenities", "transit_stops"})

#: The layers a fixture carries. Fewer than `DEFAULT_LAYER_TABLES` on purpose: freezing a
#: layer no scorer reads inflates the corridor extract for nothing, and the cap on
#: committed fixture size is what keeps the golden suite fast.
#:
#: `railways` and `flowlines` joined when `hazards` landed and started reading them.
#: `boundaries` joined in M4, when jurisdiction discovery stopped being something only a
#: region build did. The note that used to stand here - "no *scorer* reads it ... both of
#: which have a live database" - was true until `closures` needed to know whose closure
#: feed to consult, and a golden route has no database at all.
DEFAULT_LAYERS: tuple[str, ...] = (
    "ways",
    "nodes",
    "amenities",
    "parks",
    "railways",
    "flowlines",
    "transit_stops",
    "boundaries",
)

#: Boundary levels a fixture carries. States are excluded deliberately: every row of
#: `tiger.boundaries` carries `statefp`, so a route's state is a column read off the
#: counties it crosses, and a state polygon is megabytes of coastline for a fact already
#: in hand. `FROZEN_BOUNDARY_LEVELS` is why adding `boundaries` costs kilobytes.
FROZEN_BOUNDARY_LEVELS = frozenset({"county", "place"})


def _everything_in_corridor(store: PostGISLayerStore, layer: str, box: Corridor) -> GeoDataFrame:
    """Every feature of a layer meeting the corridor, whatever its geometry type.

    Point layers are asked with no `kinds` filter, so the fixture holds what a scorer
    *could* ask for rather than only what today's scorers do — a fixture pruned to the
    current kind lists would have to be re-frozen every time a scorer learns a new one.
    """
    if layer in POINT_LAYERS:
        return store.points_in_corridor(box, [], layer=layer)
    frame = store.polygons_intersecting(box, layer)
    if layer == "boundaries" and "level" in frame.columns:
        # See FROZEN_BOUNDARY_LEVELS: a state polygon is the whole coastline of California
        # to establish a fact `statefp` already carries on every county row.
        frame = frame[frame["level"].isin(FROZEN_BOUNDARY_LEVELS)]
    return frame


def _write_layer(frame: GeoDataFrame, path: Path) -> int:
    """Write one layer, dropping columns GeoPackage cannot carry.

    Two kinds of column go: values GDAL cannot hold (below), and **names that differ only
    in case**. A GeoPackage is SQLite and its column names are case-insensitive, so a
    corridor carrying both OSM's `fixme` and `FIXME` - which a wide one always does -
    fails the whole write with `FieldError: Error adding field 'FIXME'`. The first spelling
    wins, which is the one a scorer asking `tags.get("fixme")` would have read anyway.
    """
    seen: set[str] = set()
    keep: list[str] = []
    for column in frame.columns:
        if column == frame.geometry.name:
            keep.append(column)
            continue
        lowered = column.lower()
        if lowered in seen or not _is_simple(frame[column]):
            continue
        seen.add(lowered)
        keep.append(column)
    trimmed = frame[keep]
    if path.exists():
        path.unlink()
    trimmed.to_file(path, driver="GPKG")
    return len(trimmed)


def _is_simple(column: Any) -> bool:
    """Whether a column holds scalars a GeoPackage column can hold.

    A nested `jsonb` value survives tag flattening as a dict, and GDAL will either refuse
    it or stringify it inconsistently. Dropping it is honest; a scorer reading it would
    have to handle the same thing coming back as a string from the fixture and as a dict
    from the database, which is exactly the divergence the equivalence test forbids.
    """
    return not any(isinstance(value, dict | list) for value in column.head(50))


def _clip_dem(source: str, box: Corridor, path: Path) -> float:
    """Window the DEM to the corridor bbox, and report its ground resolution in metres.

    The resolution is a manifest pin, not a detail: scope 6.4 lets the ray-cast degrade
    to a coarser raster under the time budget, so a plan has to record which rung it
    actually ran on.
    """
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(source) as src:
        # Snapped to whole source pixels. An unsnapped float window rounds on read, so two
        # clips of the same raster with slightly different bounds can land on a different
        # pixel grid - and then the same route samples different cells and reports a
        # different elevation gain. A frozen fixture has to be reproducible from its bounds.
        window = from_bounds(
            box.bbox.min_lon, box.bbox.min_lat, box.bbox.max_lon, box.bbox.max_lat, src.transform
        )
        window = window.round_offsets(op="floor").round_lengths(op="ceil")
        array = src.read(1, window=window, boundless=True, fill_value=src.nodata)
        profile = src.profile | {
            "height": array.shape[0],
            "width": array.shape[1],
            "transform": src.window_transform(window),
            "compress": "lzw",
            "predictor": 3,
            "driver": "GTiff",
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(array, 1)
        # Shared with FileRasterStore so a frozen fixture's recorded resolution and the
        # one a later run reads back are the same number, converted the same way.
        return _resolution_m(src) or 0.0


def freeze_fixture(
    gpx_path: Path = typer.Argument(..., help="Route whose corridor to freeze."),
    out: Path = typer.Option(..., "--out", help="Fixture directory to write."),
    dsn: str | None = typer.Option(None, "--dsn", help="PostGIS connection string."),
    schema: str | None = typer.Option(
        None, "--schema", help="Read every layer from this schema instead of its default."
    ),
    layers: str = typer.Option(
        ",".join(DEFAULT_LAYERS), "--layers", help="Comma-separated layers to freeze."
    ),
    buffer_m: float = typer.Option(400.0, "--buffer-m", help="Corridor buffer (scope 5)."),
    dem: str | None = typer.Option(None, "--dem", help="DEM to clip: a path or a /vsicurl/ URL."),
) -> None:
    """Freeze a route's corridor from PostGIS into committed fixtures."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - psycopg is a declared dependency
        typer.echo("error: psycopg is not installed", err=True)
        raise typer.Exit(code=2) from exc

    try:
        route = gpx_read(gpx_path, route_id=gpx_path.stem)
    except GpxError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    wanted = [name.strip() for name in layers.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in DEFAULT_LAYER_TABLES]
    if unknown:
        typer.echo(
            f"error: no table mapped for {', '.join(unknown)}; "
            f"known layers are {', '.join(sorted(DEFAULT_LAYER_TABLES))}",
            err=True,
        )
        raise typer.Exit(code=2)

    target = dsn or dsn_from_env()
    try:
        connection = psycopg.connect(target, connect_timeout=10)
    except psycopg.OperationalError as exc:
        typer.echo(f"error: could not connect to {target}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    out.mkdir(parents=True, exist_ok=True)
    box = corridor(route, buffer_m=buffer_m)
    pins = SnapshotPins()

    with connection:
        # `--schema` is the option `PostGISLayerStore`'s `tables` parameter exists for: a
        # region build loading into a staging schema, or a test working in a throwaway
        # one, without a code change and without a second set of SQL.
        tables = (
            {
                name: f"{schema}.{table.partition('.')[2]}"
                for name, table in DEFAULT_LAYER_TABLES.items()
            }
            if schema
            else None
        )
        store = PostGISLayerStore(connection, tables=tables)
        for layer in wanted:
            # A layer that does not exist is reported and skipped, never written as an
            # empty file: an empty GeoPackage in a fixture is indistinguishable from a
            # corridor that genuinely contains nothing, and scope 3.6 needs those apart.
            if not store.has_layer(layer):
                typer.echo(f"skipped {layer}: no such table in this database")
                continue
            count = _write_layer(_everything_in_corridor(store, layer, box), out / f"{layer}.gpkg")
            vintage = store.vintage(layer)
            if vintage is not None:
                pins.layer_vintages[layer] = vintage
                # Scope 6.4 asks for the extract date by name, and until the OSM loader
                # existed there was nothing to read it from. `ways` is the layer that
                # carries it: every other OSM layer came out of the same file.
                if layer == "ways":
                    pins.osm_extract_date = vintage
            typer.echo(f"froze {layer}: {count} features, vintage {vintage or 'unrecorded'}")

    if dem:
        pins.dem_resolution_m = _clip_dem(dem, box, out / "dem.tif")
        typer.echo(f"froze dem: {pins.dem_resolution_m:.0f} m resolution")

    snapshot = out.parent / "snapshot.json"
    merged = _merge_pins(snapshot, pins)
    snapshot.write_text(merged.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(f"wrote {snapshot}")


def _merge_pins(snapshot: Path, fresh: SnapshotPins) -> SnapshotPins:
    """Fold this run's pins into whatever the route already recorded.

    A freeze names its layers, so most runs re-freeze *some* of them. Writing the pins
    whole would then silently drop every pin this run did not produce — re-freezing the
    vector layers would erase `dem_resolution_m`, which scope 6.4 requires the manifest to
    carry and which nothing downstream would notice was missing.

    A route's `snapshot.json` also carries hand-written `extra` notes ("canopy not fetched:
    the tile index is 15 MB and this corridor is downtown"). Those are provenance a
    reviewer wrote, and no freeze should be able to delete them. Delete the file to start
    clean; that is the deliberate act, and overwriting is not.
    """
    if not snapshot.exists():
        return fresh

    try:
        previous = SnapshotPins.model_validate_json(snapshot.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        typer.echo(f"warning: could not read {snapshot}, replacing it: {exc}", err=True)
        return fresh

    merged = previous.model_copy(deep=True)
    merged.layer_vintages.update(fresh.layer_vintages)
    merged.extra.update(fresh.extra)
    if fresh.dem_resolution_m is not None:
        merged.dem_resolution_m = fresh.dem_resolution_m
    if fresh.canopy_version is not None:
        merged.canopy_version = fresh.canopy_version
    if fresh.osm_extract_date is not None:
        merged.osm_extract_date = fresh.osm_extract_date
    return merged


def freeze_cassette(
    gpx_path: Path = typer.Argument(..., help="Route whose forecast to record."),
    date: datetime = typer.Option(..., "--date", formats=["%Y-%m-%d"], help="Run date."),
    out: Path = typer.Option(..., "--out", help="Cassette file to write (cache.sqlite)."),
    spacing_m: float = typer.Option(
        DEFAULT_SPACING_M, "--spacing-m", help="Distance between forecast sites."
    ),
) -> None:
    """Record a route's forecast into a cassette a golden run can replay.

    The same argument as `freeze-fixture`: this calls the *same* `route_forecast` a scorer
    calls, so the cassette is literally what the API answered and holds exactly the keys a
    scorer will ask for. A hand-written one would be a second implementation of the key
    derivation and would drift the first time a rounding rule changed.

    A forecast for a past date can never be re-fetched, so record before the day arrives
    and treat the file as permanent. That is also why a golden pins an absolute date.
    """
    try:
        route = gpx_read(gpx_path, route_id=gpx_path.stem)
    except GpxError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    out.parent.mkdir(parents=True, exist_ok=True)
    with SqliteCache(out, offline=False) as cache:
        ctx = ScorerContext(
            layers=FileLayerStore(gpx_path.parent),
            rasters=FileRasterStore(gpx_path.parent),
            cache=cache,
            clock=FrozenClock(date),
            coverage=CoverageManifest(),
            profile=PreferenceProfile(),
            budget=Budget(),
        )
        forecast = route_forecast(route, ctx, date.date(), spacing_m=spacing_m)
        # Air quality is a second endpoint on its own grid, so it is a second set of keys.
        # Recorded here rather than in a separate command because a golden needs both, and
        # a cassette that carries one and not the other is a route that half-scores.
        air = route_air_quality(route, ctx, date.date())
        recorded = len(cache.keys())

    for site in forecast.sites:
        state = site.provider if site.hours else f"none ({site.reason})"
        typer.echo(f"  site {site.site.index} at {site.site.cum_dist_m / 1000:.1f} km: {state}")
    typer.echo(f"  air quality: {sum(1 for s in air.sites if s.hours)} of {len(air.sites)} site(s)")
    typer.echo(f"wrote {out}: {recorded} key(s), {ctx.budget.api_calls_used} API call(s)")
    if not forecast.answered and not air.answered:
        typer.echo("error: nothing was recorded", err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.command("freeze-fixture")(freeze_fixture)
    app.command("freeze-cassette")(freeze_cassette)
