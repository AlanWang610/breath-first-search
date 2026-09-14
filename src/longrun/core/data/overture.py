"""Building footprints and heights from Overture, over S3 (scope 5, 13).

Overture publishes GeoParquet on a public S3 bucket, partitioned by theme. duckdb's spatial
extension reads it directly with a bounding-box filter, so a corridor's buildings come back
without staging a national extract — which is what makes building heights reachable in M2
at all, well before the scope 13 region build exists.

**Nothing in `core/` imports this.** It is ingest: `dsm.py` reads buildings through
`LayerStore.polygons_intersecting`, from a GeoPackage a freeze wrote. Two reasons that
separation is not fussiness. `conftest._block_network` fails any non-`network` test that
opens an off-host socket, so a `dsm.py` that reached S3 would make every golden route
scoring `sun_exposure` unrunnable in CI. And duckdb lives in the `ingest` extra, while
`core/` has to import on a bare `uv sync`.

So the path is: this module writes a GeoPackage, `FileLayerStore` reads it, and the DSM
never knows S3 exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from longrun.core.models.geometry import BBox

S3_ROOT = "s3://overturemaps-us-west-2/release"

#: Pinned rather than "latest" on purpose. Scope 6.4 wants the manifest to record which
#: vintage a plan was built from, and a floating release makes two plans incomparable for
#: a reason neither of them records.
DEFAULT_RELEASE = "2026-08-19.0"

#: Overture's buildings theme. `height` is metres above ground where present, and it is
#: frequently absent — `dsm.building_height_m` is what decides what to do about that.
BUILDINGS_PATH = "theme=buildings/type=building/*"

#: Columns worth carrying into a fixture. Everything else in the schema is provenance and
#: naming that a shade calculation has no use for, and a corridor of downtown buildings is
#: large enough that the difference matters.
COLUMNS = ("id", "height", "num_floors", "geometry")


def buildings_query(bbox: BBox, release: str = DEFAULT_RELEASE) -> str:
    """The duckdb SQL that pulls one corridor's buildings out of Overture.

    Filtered on the published `bbox` struct rather than on the geometry, because that is
    what the Parquet row groups are ordered by: a spatial predicate over the geometry
    column would read the whole theme to answer a question about one city.
    """
    columns = ", ".join(c for c in COLUMNS if c != "geometry")
    return f"""
        SELECT {columns}, ST_AsWKB(geometry) AS wkb
        FROM read_parquet('{S3_ROOT}/{release}/{BUILDINGS_PATH}', filename = true,
                          hive_partitioning = 1)
        WHERE bbox.xmin BETWEEN {bbox.min_lon} AND {bbox.max_lon}
          AND bbox.ymin BETWEEN {bbox.min_lat} AND {bbox.max_lat}
    """


def fetch_buildings(bbox: BBox, release: str = DEFAULT_RELEASE) -> Any:
    """A GeoDataFrame of building footprints with heights, in WGS84.

    Needs the `ingest` extra (duckdb) and the network. `network`-marked wherever it is
    exercised.
    """
    import duckdb
    import geopandas as gpd
    from shapely import from_wkb

    connection = duckdb.connect()
    connection.execute("INSTALL spatial; LOAD spatial;")
    connection.execute("INSTALL httpfs; LOAD httpfs;")
    connection.execute("SET s3_region='us-west-2';")

    rows = connection.execute(buildings_query(bbox, release)).fetchall()
    names = [c for c in COLUMNS if c != "geometry"]

    records = [dict(zip(names, row[: len(names)], strict=True)) for row in rows]
    geometries = [from_wkb(bytes(row[len(names)])) for row in rows]
    connection.close()

    if not records:
        return gpd.GeoDataFrame({name: [] for name in names}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def freeze_buildings(bbox: BBox, path: Path, release: str = DEFAULT_RELEASE) -> int:
    """Write a corridor's buildings to a GeoPackage a `FileLayerStore` can serve."""
    frame = fetch_buildings(bbox, release)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_file(path, driver="GPKG")
    return len(frame)


__all__ = [
    "BUILDINGS_PATH",
    "COLUMNS",
    "DEFAULT_RELEASE",
    "S3_ROOT",
    "buildings_query",
    "fetch_buildings",
    "freeze_buildings",
]
