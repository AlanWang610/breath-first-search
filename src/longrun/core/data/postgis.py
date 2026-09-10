"""`LayerStore` against the real database (scope 4.4, 5, 13).

The second implementation of the interface in `core.data.base`, and the reason that seam
exists. `FileLayerStore` is what the test suite runs on; this is what a region build feeds
and what a fixture freeze reads to *produce* those fixtures, so a committed GeoPackage is
literally the answer PostGIS gave rather than a hand-drawn approximation of it.

Three things are deliberate.

**The corridor polygon is built in Python, not in SQL.** `core.geo.segments.corridor_polygon`
is shared with the file store, so both implementations query the identical geometry and the
`network`-marked equivalence test isolates *query* differences instead of quietly comparing
two different questions.

**Tags are flattened.** Scorers read a way's tags as columns (`row["highway"]`), because
that is what a GeoPackage gives them. osm2pgsql keeps them in one `jsonb` column, so this
expands that column into the flat shape the scorers already expect. Doing it here rather
than in every scorer is what keeps the seam a seam.

**A missing table raises `LayerNotFound`.** "No water fountains in the corridor" and "nobody
ever loaded the water layer" are different claims and only the first is evidence (scope
3.6), so an absent table can never reach a scorer as an empty result.

The tables named below are the contract the `regions/build.py` loaders (M3) must satisfy.
Nothing creates them yet.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor_polygon

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from geopandas import GeoDataFrame

    from longrun.core.models.geometry import Corridor, Route

WGS84 = 4326

#: Where the development database lives. Defined here rather than in each caller so the
#: CLI and the contract tests cannot drift apart on it. The password is the username; the
#: compose file binds the port to 127.0.0.1, which is what makes that acceptable.
DSN_ENV_VAR = "LONGRUN_POSTGIS_DSN"
DEFAULT_DSN = "postgresql://longrun:longrun@localhost:5432/longrun"

#: Layer name -> qualified table, one schema per layer group as the init SQL creates them.
#: Overridable per store so a region build can point at a staging schema without a code
#: change.
DEFAULT_LAYER_TABLES: dict[str, str] = {
    "ways": "osm.ways",
    "nodes": "osm.nodes",
    "amenities": "osm.amenities",
    "railways": "osm.railways",
    "parks": "padus.units",
    "buildings": "overture.buildings",
    "flowlines": "nhd.flowlines",
    "boundaries": "tiger.boundaries",
    # Service *summaries* per stop, not a timetable: every LayerStore method is a
    # corridor query, so precomputing at build time is what makes transit reachable
    # through the seam at all. See core/data/gtfs.py.
    "transit_stops": "gtfs.stops",
}

#: Geometry column every layer table carries, in EPSG:4326.
GEOM_COLUMN = "geom"

#: Column holding OSM tags as `jsonb`, expanded into flat columns on read. Absent on
#: layers that have no tags (PAD-US units, TIGER boundaries), which is not an error.
TAGS_COLUMN = "tags"


class PostGISLayerStore:
    """Vector layers read from PostGIS. Satisfies `core.data.base.LayerStore`."""

    def __init__(self, connection: Any, tables: dict[str, str] | None = None) -> None:
        """Wrap an open psycopg connection.

        The connection is injected rather than opened here: a region build runs many
        queries inside one transaction, and a store that owned its own connection would
        either leak one per instance or commit in the middle of a build.
        """
        self._conn = connection
        self._tables = dict(DEFAULT_LAYER_TABLES) if tables is None else dict(tables)
        self._vintages: dict[str, str] = {}

    # --- layer resolution ---------------------------------------------------

    def _exists(self, table: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
            row = cur.fetchone()
        return bool(row and row[0])

    def has_layer(self, layer: str) -> bool:
        """Whether the layer's table exists at all (scope 3.6)."""
        table = self._tables.get(layer)
        return False if table is None else self._exists(table)

    def _require_table(self, layer: str) -> str:
        table = self._tables.get(layer)
        if table is None:
            raise LayerNotFound(f"no table mapped for layer {layer!r}")
        if not self._exists(table):
            raise LayerNotFound(f"no {layer!r} layer: table {table} does not exist")
        return table

    def _columns(self, table: str) -> list[str]:
        schema, _, name = table.partition(".")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, name),
            )
            return [row[0] for row in cur.fetchall()]

    # --- queries ------------------------------------------------------------

    def ways_in_corridor(self, corridor: Corridor) -> GeoDataFrame:
        return self._intersecting("ways", corridor_polygon(corridor).wkt)

    def points_in_corridor(
        self, corridor: Corridor, kinds: list[str], layer: str = "amenities"
    ) -> GeoDataFrame:
        return self._intersecting(layer, corridor_polygon(corridor).wkt, kinds=kinds)

    def polygons_intersecting(self, corridor: Corridor, layer: str) -> GeoDataFrame:
        return self._intersecting(layer, corridor_polygon(corridor).wkt)

    def lines_crossing(self, route: Route, layer: str) -> GeoDataFrame:
        """Features the route line itself crosses, not the corridor around it."""
        line = "LINESTRING(" + ", ".join(f"{p.lon} {p.lat}" for p in route.points) + ")"
        return self._intersecting(layer, line)

    def _intersecting(
        self, layer: str, geometry_wkt: str, kinds: list[str] | None = None
    ) -> GeoDataFrame:
        """Index pass then exact predicate, which is what `ST_Intersects` plans to.

        The GIST index answers the bounding-box stage and the exact predicate filters what
        survives - the same two-stage shape `FileLayerStore._clip` imitates with an STRtree.
        """
        table = self._require_table(layer)
        columns = self._columns(table)
        selected = [c for c in columns if c != GEOM_COLUMN]

        projection = ", ".join(f'"{c}"' for c in selected)
        sql = (
            f"SELECT {projection + ', ' if projection else ''}"
            f'ST_AsBinary("{GEOM_COLUMN}") AS wkb '
            f"FROM {table} "
            f'WHERE ST_Intersects("{GEOM_COLUMN}", ST_GeomFromText(%s, {WGS84}))'
        )
        params: list[Any] = [geometry_wkt]
        if kinds and "kind" in columns:
            sql += " AND kind = ANY(%s)"
            params.append(list(kinds))

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return _to_geodataframe(rows, [*selected, "wkb"])

    # --- manifest -----------------------------------------------------------

    def vintage(self, layer: str) -> str | None:
        """Source vintage for the manifest's snapshot pins (scope 6.4).

        Read from `meta.layer_vintage`, which the loaders write at load time, so the
        manifest reports what the database actually holds rather than what a loader
        remembered to report. A region-specific row wins over a nationally loaded one,
        because the region build is the more recent and more specific statement.
        """
        if layer in self._vintages:
            return self._vintages[layer]
        table = self._tables.get(layer)
        if table is None:
            return None
        schema = table.partition(".")[0]
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT vintage FROM meta.layer_vintage WHERE layer_schema = %s "
                "ORDER BY (region = '') ASC, loaded_at DESC LIMIT 1",
                (schema,),
            )
            row = cur.fetchone()
        return None if row is None else str(row[0])

    def set_vintage(self, layer: str, vintage: str) -> None:
        self._vintages[layer] = vintage


def _to_geodataframe(rows: list[Any], columns: list[str]) -> GeoDataFrame:
    """Build a WGS84 GeoDataFrame, expanding a `tags` column into flat columns.

    Scorers read tags as columns because that is what a GeoPackage gives them; PostGIS
    keeps them in one `jsonb`. Expanding here rather than in each scorer is what lets one
    scorer run unchanged against either store.
    """
    import geopandas as gpd
    from shapely import from_wkb

    records: list[dict[str, Any]] = []
    geometries: list[Any] = []

    for row in rows:
        record = dict(zip(columns, row, strict=True))
        wkb = record.pop("wkb")
        geometries.append(None if wkb is None else from_wkb(bytes(wkb)))
        tags = record.pop(TAGS_COLUMN, None)
        if isinstance(tags, dict):
            # A real column wins over a tag of the same name: `way_id` is the join key,
            # and a stray `tags->>'way_id'` must not be able to displace it.
            record = {**tags, **record}
        records.append(record)

    if not records:
        empty = [c for c in columns if c != "wkb"]
        return gpd.GeoDataFrame({c: [] for c in empty}, geometry=[], crs=f"EPSG:{WGS84}")
    return gpd.GeoDataFrame(records, geometry=geometries, crs=f"EPSG:{WGS84}")


def dsn_from_env(env: Mapping[str, str] | None = None) -> str:
    """The database to connect to, from the environment or the development default."""
    source = os.environ if env is None else env
    return source.get(DSN_ENV_VAR, "").strip() or DEFAULT_DSN


__all__ = [
    "DEFAULT_DSN",
    "DEFAULT_LAYER_TABLES",
    "DSN_ENV_VAR",
    "PostGISLayerStore",
    "dsn_from_env",
]
