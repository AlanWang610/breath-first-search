"""OSM into PostGIS — step 1 of the scope 13 region build (ADR 0009, ADR 0010).

This is the gap M1.7b named and M2 did not close. `PostGISLayerStore.DEFAULT_LAYER_TABLES`
maps `ways` to `osm.ways` and seven siblings, `longrun freeze-fixture` is written and
tested against a live database, and until now **nothing created those tables** — so six
scorers reported `unavailable` on a real corridor for want of a layer, not for want of an
answer.

**Nothing in `core/` imports this**, exactly as with `overture.py`. `osmium` lives in the
`ingest` extra while `core/` must import on a bare `uv sync`, and `conftest._block_network`
fails any non-`network` test that opens an off-host socket. The path is: this module writes
PostGIS, `freeze-fixture` writes a GeoPackage from it, `FileLayerStore` serves that, and no
scorer knows a `.pbf` exists.

The split inside the module matters for the same reason. The **vocabulary** — which tags
make a way worth keeping, which `amenity` value is a water source, which `barrier` stops a
runner — is pure, is what a scorer's answer depends on, and is unit-tested in CI. The
**reading and writing** needs osmium, a 233 MB file and a database, and is `network`-marked.

Two shapes here are measurements rather than taste, and ADR 0010 records both:

* **The tag filter runs in osmium's C++ chain, not in Python.** Testing `"highway" in
  dict(obj.tags)` on the Python side materialises an object for each of the 2.55 M Bay Area
  ways that are then thrown away: 70.7 s against 15.1 s for the identical 938,678 ways out.
* **Geometry goes to `COPY` as the hex the factory produced.** Decoding it to `bytes` at
  parse time and re-encoding at insert time cost 130 s for nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator
    from pathlib import Path

WGS84 = 4326

#: Default schema. Overridable so a build can stage into a scratch schema and swap, the
#: same parameter `PostGISLayerStore(tables=...)` and `freeze-fixture --schema` exist for.
DEFAULT_SCHEMA = "osm"

#: A way is worth keeping if a segment could lie on it or a scorer could read it. Both
#: `highway` and `railway` are here: `legality._check_railway` needs to see a bare
#: `railway=rail` linestring to call it trespass, and it can only see one that was loaded.
#:
#: This tuple is passed to `osmium.filter.KeyFilter`, so it is also the C++-side filter —
#: a key added here costs a full reload, which is why the vocabularies below are
#: deliberately a little wider than today's scorers ask for.
WAY_KEYS: tuple[str, ...] = ("highway", "railway", "footway", "cycleway")

#: Rail values that make a way a railway line in its own right, for the `railways` layer
#: `hazards` reads with `lines_crossing`. Deliberately excludes `disused`, `abandoned`,
#: `razed` and `construction`: an at-grade crossing of a lifted line is not a hazard, and
#: `legality.RAILWAY_ROW` draws the same distinction for the same reason.
RAILWAY_VALUES: frozenset[str] = frozenset(
    {"rail", "light_rail", "subway", "tram", "monorail", "narrow_gauge", "funicular"}
)

#: Point features that stop or slow a runner, for `osm.nodes`. `crossings.SIGNAL_KINDS`
#: and `stop_density.STOP_KINDS` name what they ask for; this is that plus the neighbours
#: it would be absurd to have to reload a region to add.
NODE_KINDS: dict[str, frozenset[str]] = {
    "highway": frozenset({"traffic_signals", "crossing", "stop", "give_way", "mini_roundabout"}),
    "railway": frozenset({"level_crossing", "crossing"}),
    "barrier": frozenset(
        {"gate", "lift_gate", "swing_gate", "kissing_gate", "stile", "bollard", "cycle_barrier"}
    ),
}

#: Resupply, for `osm.amenities`. `services.SERVICE_CATEGORIES` maps these to water, toilet
#: and food; the extras (`bakery`, `greengrocer`, `marketplace`, `water_tap`) are loaded
#: unclassified so that teaching a scorer a new kind is a code change and not a reload.
AMENITY_KINDS: dict[str, frozenset[str]] = {
    "amenity": frozenset(
        {
            "drinking_water",
            "water_point",
            "fountain",
            "toilets",
            "cafe",
            "fast_food",
            "restaurant",
            "marketplace",
        }
    ),
    "shop": frozenset(
        {"convenience", "supermarket", "deli", "greengrocer", "bakery", "general", "kiosk"}
    ),
    "man_made": frozenset({"water_tap", "drinking_fountain"}),
}

#: Keys that can make a *node* a row, for the C++-side filter on the node pass. The union
#: of both vocabularies: one pass classifies into `nodes` or `amenities` afterwards, since
#: a node can only be one of the two and reading the file twice to find out would cost more
#: than the handful of nodes that carry a key and no matching value.
POINT_KEYS: tuple[str, ...] = tuple(dict.fromkeys([*NODE_KINDS, *AMENITY_KINDS]))

#: Keys that can make a *closed way* an amenity. Narrower than `POINT_KEYS` on purpose:
#: `highway` is in there, and a way pass filtered on it would readmit every road.
AREA_KEYS: tuple[str, ...] = tuple(AMENITY_KINDS)


# --- vocabulary: pure, and what a scorer's answer depends on ----------------


def way_is_wanted(tags: dict[str, Any]) -> bool:
    """Whether a way is worth a row. Mirrors the C++ filter, and is what tests it."""
    return any(key in tags for key in WAY_KEYS)


def is_railway_line(tags: dict[str, Any]) -> bool:
    """Whether a way is a live railway line rather than a road with tracks in it.

    A tram on a street is `railway=tram` *and* `highway=*`, and a runner crosses it at
    grade like any other road marking. A bare `railway=rail` linestring is a right of way.
    """
    if "highway" in tags:
        return False
    return str(tags.get("railway", "")).strip().lower() in RAILWAY_VALUES


def _kind_from(tags: dict[str, Any], vocabulary: dict[str, frozenset[str]]) -> str | None:
    """First tag value that appears in the vocabulary, in declaration order.

    Order is the resolution rule, not an accident: a `shop=convenience` counter inside a
    building tagged `amenity=cafe` reads as the cafe, because `amenity` is declared first
    and it is the tag on the object being asked about.
    """
    for key, values in vocabulary.items():
        value = str(tags.get(key, "")).strip().lower()
        if value in values:
            return value
    return None


def node_kind(tags: dict[str, Any]) -> str | None:
    """The `kind` column for a control node, or None if it is not one.

    `kind` carries the raw OSM value because that is what the scorers match on —
    `crossings.is_signalized` tests `kind == "traffic_signals"`. Normalising here would put
    a translation table between the tag and the test, in a place no test can see.
    """
    return _kind_from(tags, NODE_KINDS)


def amenity_kind(tags: dict[str, Any]) -> str | None:
    """The `kind` column for a service point, or None if it is not one."""
    return _kind_from(tags, AMENITY_KINDS)


# --- schema -----------------------------------------------------------------

#: Column definitions per table, less the geometry, which varies by type. Tags are `jsonb`
#: because that is what `PostGISLayerStore` expands into the flat columns a scorer reads —
#: the one place OSM's open tag space meets a fixed schema.
TABLE_COLUMNS: dict[str, str] = {
    "ways": "way_id bigint PRIMARY KEY, tags jsonb NOT NULL",
    "railways": "way_id bigint PRIMARY KEY, tags jsonb NOT NULL",
    "nodes": "node_id bigint PRIMARY KEY, kind text NOT NULL, tags jsonb NOT NULL",
    "amenities": "osm_id bigint PRIMARY KEY, kind text NOT NULL, tags jsonb NOT NULL",
}

#: PostGIS geometry type per table.
TABLE_GEOMETRY: dict[str, str] = {
    "ways": "LineString",
    "railways": "LineString",
    "nodes": "Point",
    "amenities": "Point",
}

#: Primary key column per table, which is also the upsert conflict target.
TABLE_KEY: dict[str, str] = {
    "ways": "way_id",
    "railways": "way_id",
    "nodes": "node_id",
    "amenities": "osm_id",
}

TABLES: tuple[str, ...] = tuple(TABLE_COLUMNS)


def create_statements(schema: str = DEFAULT_SCHEMA) -> list[str]:
    """DDL for the four OSM tables, idempotent and in dependency order.

    The GIST index is the convention `deploy/postgis/init/01-schemas.sql` records, and it
    is the index that decides whether the scope 6.4 budget is met: every scorer begins by
    intersecting a corridor buffer against a layer.
    """
    statements: list[str] = [f"CREATE SCHEMA IF NOT EXISTS {schema}"]
    for table in TABLES:
        columns = TABLE_COLUMNS[table]
        geometry = TABLE_GEOMETRY[table]
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {schema}.{table} "
            f"({columns}, geom geometry({geometry}, {WGS84}) NOT NULL)"
        )
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {table}_geom_idx ON {schema}.{table} USING GIST (geom)"
        )
    # `kind` is the only non-spatial predicate a scorer sends: `points_in_corridor` filters
    # on it, and without this the corridor rows are all re-read to discard most of them.
    for table in ("nodes", "amenities"):
        statements.append(f"CREATE INDEX IF NOT EXISTS {table}_kind_idx ON {schema}.{table} (kind)")
    return statements


# --- reading the extract ----------------------------------------------------


@dataclass(frozen=True)
class OsmRow:
    """One row bound for one table, with geometry already in WGS84 hex WKB."""

    table: str
    osm_id: int
    kind: str | None
    tags: dict[str, Any]
    wkb_hex: str


@dataclass
class LoadReport:
    """What a load did, for the build manifest and for the operator watching it."""

    vintage: str
    counts: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def extract_vintage(path: Path) -> str:
    """The extract's own timestamp, for `meta.layer_vintage`.

    Read from the pbf header rather than from the file's mtime: a copied or re-downloaded
    file has a new mtime and the same data, and scope 6.4 wants a plan's sources pinned to
    something two plans can be compared on. Falls back to the modification date when a
    producer wrote no replication timestamp, which some clipped extracts do not.
    """
    from datetime import UTC, datetime

    import osmium

    reader = osmium.io.Reader(str(path))
    try:
        header = reader.header()
        for key in ("osmosis_replication_timestamp", "timestamp"):
            value = header.get(key, "")
            if value:
                return str(value)
    finally:
        reader.close()

    return datetime.fromtimestamp(path.stat().st_mtime, UTC).date().isoformat()


def read_ways(path: Path) -> Iterator[OsmRow]:
    """Ways and railway lines, filtered in osmium's C++ chain.

    `with_locations` is what turns a way's node list into coordinates, and it is the reason
    nodes cannot also be filtered away here: the location cache has to see every node in
    the file even though almost none of them become a row.
    """
    import osmium
    from osmium.geom import WKBFactory

    factory = WKBFactory()
    processor = (
        osmium.FileProcessor(str(path))
        .with_locations("flex_mem")
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter(*WAY_KEYS))
    )
    for obj in processor:
        # The EntityFilter above already guarantees this; the isinstance is what tells a
        # type checker so, and it costs one pointer comparison per way.
        if not isinstance(obj, osmium.osm.Way):  # pragma: no cover - filtered upstream
            continue
        tags = dict(obj.tags)
        try:
            wkb = factory.create_linestring(obj)
        except (osmium.InvalidLocationError, RuntimeError):
            # A way whose nodes fall outside the extract. Skipped rather than partially
            # reconstructed: half a linestring is a geometry that answers corridor queries
            # wrongly rather than not at all.
            continue
        table = "railways" if is_railway_line(tags) else "ways"
        yield OsmRow(table=table, osm_id=obj.id, kind=None, tags=tags, wkb_hex=wkb)


def read_points(path: Path) -> Iterator[OsmRow]:
    """Control nodes and service points, from nodes and from closed ways.

    Ways matter here and it is not a nicety: a supermarket, a park toilet block and most
    cafes are mapped as building polygons, so a nodes-only load would find the drinking
    fountains and miss almost every shop. Their centroid is what a `points_in_corridor`
    query is asking for anyway — "is there water within 200 m of the line".

    **Two filtered passes, not one unfiltered one.** A single pass has to see all 30.2 M
    Bay Area nodes in Python to decide which are control nodes, which is the same mistake
    ADR 0010 records for the ways pass and costs more here because there are ten times as
    many nodes. Split, each pass carries a `KeyFilter` and the second half of the file is
    never built into Python objects. The node pass also drops `with_locations`, which is
    pure overhead when every object already carries its own coordinate.

    Multipolygon *relations* are not handled. A relation-mapped shop is rare enough that
    the honest move is to say so here rather than to pull in the area assembler and its
    memory profile for it.
    """
    import osmium
    from osmium.geom import WKBFactory
    from shapely import from_wkb
    from shapely.geometry import Polygon

    factory = WKBFactory()

    nodes = (
        osmium.FileProcessor(str(path))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.NODE))
        .with_filter(osmium.filter.KeyFilter(*POINT_KEYS))
    )
    for obj in nodes:
        if not isinstance(obj, osmium.osm.Node):  # pragma: no cover - filtered upstream
            continue
        tags = dict(obj.tags)
        control = node_kind(tags)
        kind = control or amenity_kind(tags)
        if kind is None:
            continue
        yield OsmRow(
            table="nodes" if control is not None else "amenities",
            osm_id=obj.id,
            kind=kind,
            tags=tags,
            wkb_hex=factory.create_point(obj),
        )

    areas = (
        osmium.FileProcessor(str(path))
        .with_locations("flex_mem")
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter(*AREA_KEYS))
    )
    for obj in areas:
        if not isinstance(obj, osmium.osm.Way):  # pragma: no cover - filtered upstream
            continue
        tags = dict(obj.tags)
        kind = amenity_kind(tags)
        if kind is None:
            continue
        try:
            line = from_wkb(bytes.fromhex(factory.create_linestring(obj)))
        except (osmium.InvalidLocationError, RuntimeError, ValueError):
            continue
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        # A closed way is an area, and its area centroid is where the shop is. An open way
        # tagged as a shop is a mapping error, and its midpoint is the honest guess.
        closed = coords[0] == coords[-1] and len(coords) >= 4
        shape = Polygon(coords).centroid if closed else line.interpolate(0.5, normalized=True)
        yield OsmRow(
            table="amenities",
            # Negated so a way-derived amenity cannot collide with a node-derived one in
            # the same table. The sign is the provenance: recoverable and unique, which a
            # synthetic surrogate key would not be.
            osm_id=-obj.id,
            kind=kind,
            tags=tags,
            wkb_hex=shape.wkb_hex,
        )


# --- writing ----------------------------------------------------------------


#: Rows held in memory before a `COPY` flushes them. The point is that peak memory is a
#: property of this number and not of the extract: buffering a region's ways whole is ~3 GB
#: for the Bay Area and grows with every larger region, which is the same mistake ADR 0003
#: rejected for DSM tiles ("tile, never bbox") in a different shape.
BATCH_ROWS = 100_000


class _Sink:
    """Batched `COPY` into a temp table, upserted into the live one at the end.

    Staging then upserting is what makes the step idempotent, and ADR 0010 records why it
    has to be: OSM ids are global, so two regions sharing a boundary way are making the
    same claim about it and the second load should be a no-op. A straight `COPY` into the
    live table fails on the first shared way — exactly where a resumed build resumes.
    """

    def __init__(self, cursor: Any, schema: str, table: str) -> None:
        self._cursor = cursor
        self._schema = schema
        self._table = table
        self._staging = f"stage_{table}"
        self._buffer: list[OsmRow] = []
        self.written = 0

        key = TABLE_KEY[table]
        columns = TABLE_COLUMNS[table]
        self._key = key
        self._has_kind = "kind text" in columns
        self._names = [key, *(["kind"] if self._has_kind else []), "tags", "geom"]
        self._types = ["bigint", *(["text"] if self._has_kind else []), "jsonb", "text"]

        cursor.execute(f"DROP TABLE IF EXISTS pg_temp.{self._staging}")
        cursor.execute(
            f"CREATE TEMP TABLE {self._staging} "
            f"({columns.replace(' PRIMARY KEY', '')}, "
            f"geom geometry({TABLE_GEOMETRY[table]}, {WGS84}) NOT NULL)"
        )

    def add(self, row: OsmRow) -> None:
        self._buffer.append(row)
        if len(self._buffer) >= BATCH_ROWS:
            self.flush()

    def flush(self) -> None:
        from psycopg.types.json import Jsonb

        if not self._buffer:
            return
        statement = f"COPY pg_temp.{self._staging} ({', '.join(self._names)}) FROM STDIN"
        with self._cursor.copy(statement) as copy:
            copy.set_types(self._types)
            for row in self._buffer:
                values: list[Any] = [row.osm_id]
                if self._has_kind:
                    values.append(row.kind)
                # The geometry goes over as the hex the factory produced. Decoding it to
                # `bytes` and re-encoding here cost 130 s on the Bay Area for nothing.
                values.extend([Jsonb(row.tags), row.wkb_hex])
                copy.write_row(tuple(values))
        self.written += len(self._buffer)
        self._buffer.clear()

    def commit(self) -> int:
        """Upsert everything staged into the live table and drop the staging table."""
        self.flush()
        if self.written:
            updates = ", ".join(
                f"{name} = EXCLUDED.{name}" for name in self._names if name != self._key
            )
            names = ", ".join(self._names)
            # `DISTINCT ON` because an extract can carry the same id twice, and Postgres
            # refuses to update the same row twice in one statement rather than picking a
            # winner. Without it the load dies on whichever duplicate the region contains.
            self._cursor.execute(
                f"INSERT INTO {self._schema}.{self._table} ({names}) "
                f"SELECT DISTINCT ON ({self._key}) {names} FROM pg_temp.{self._staging} "
                f"ORDER BY {self._key} "
                f"ON CONFLICT ({self._key}) DO UPDATE SET {updates}"
            )
        self._cursor.execute(f"DROP TABLE IF EXISTS pg_temp.{self._staging}")
        return self.written


def load_extract(
    path: Path,
    connection: Any,
    region: str,
    schema: str = DEFAULT_SCHEMA,
    source_url: str | None = None,
    truncate: bool = False,
) -> LoadReport:
    """Load one `.pbf` into `<schema>.{ways,railways,nodes,amenities}`.

    Three filtered passes over the file rather than one unfiltered one. Each pass carries
    its tag test into osmium's C++ chain, so the objects it does not want are never built
    into Python objects at all — 2.55 M of the Bay Area's 3.49 M ways on the first pass,
    and 30.1 M of its 30.2 M nodes on the second. A single pass would have to see every one
    of them in Python to decide, which is what ADR 0010 measured at 70.7 s against 15.1 s.

    Rows stream into the database in batches, so peak memory is `BATCH_ROWS` and not the
    size of the region.

    `meta.layer_vintage` is written last, so a row saying a region loaded is only there
    when it did (scope 6.4).
    """
    started = time.perf_counter()
    vintage = extract_vintage(path)
    report = LoadReport(vintage=vintage)

    with connection.cursor() as cursor:
        for statement in create_statements(schema):
            cursor.execute(statement)
        if truncate:
            for table in TABLES:
                cursor.execute(f"TRUNCATE {schema}.{table}")

        for reader in (read_ways, read_points):
            sinks = {table: _Sink(cursor, schema, table) for table in TABLES}
            for row in reader(path):
                sinks[row.table].add(row)
            for table, sink in sinks.items():
                report.counts[table] = report.counts.get(table, 0) + sink.commit()

        cursor.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, region, vintage, source_url) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (layer_schema, source, region) DO UPDATE SET "
            "vintage = EXCLUDED.vintage, source_url = EXCLUDED.source_url, loaded_at = now()",
            (schema, "openstreetmap", region, vintage, source_url),
        )
    connection.commit()

    report.elapsed_s = time.perf_counter() - started
    return report


__all__ = [
    "AMENITY_KINDS",
    "DEFAULT_SCHEMA",
    "NODE_KINDS",
    "RAILWAY_VALUES",
    "TABLES",
    "WAY_KEYS",
    "LoadReport",
    "OsmRow",
    "amenity_kind",
    "create_statements",
    "extract_vintage",
    "is_railway_line",
    "load_extract",
    "node_kind",
    "read_points",
    "read_ways",
    "way_is_wanted",
]
