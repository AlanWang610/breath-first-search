"""A frozen fixture is what PostGIS actually answered (scope 11).

`test_layer_store_equivalence` proves the two stores agree when both are handed the same
records. This proves the step before that one: that `longrun freeze-fixture` is what puts
the records into the file store in the first place, and that nothing is lost or invented
on the way through.

The assertion is a round trip. Freeze a corridor out of the database, open the frozen
directory with `FileLayerStore`, and require it to answer the corridor queries exactly as
`PostGISLayerStore` does — down to the LTS level a scorer would read, not merely the row
count. If that holds, "the golden suite runs on fixtures" and "the golden suite reflects
the database" are the same statement.

The corridor here is deliberately its own, not shared with the equivalence test. The two
prove different things, and a shared fixture would mean editing one test's data silently
changed what the other one was asserting.

`network`-marked: needs the live database.

    docker compose -f deploy/docker-compose.yml up -d
    uv run pytest tests/contract/test_freeze_fixture.py -m network
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import LineString, Point, Polygon
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.data.file_store import FileLayerStore
from longrun.core.data.postgis import DEFAULT_LAYER_TABLES, PostGISLayerStore, dsn_from_env
from longrun.core.geo.gpx import gpx_write
from longrun.core.geo.segments import corridor
from longrun.core.models.geometry import Route, RoutePoint

pytestmark = pytest.mark.network

#: Kept out of the layer schemas so a failed run cannot leave debris where a region build
#: would load. `freeze-fixture` is pointed at it with `--schema`.
TEST_SCHEMA = "pytest_freeze"

#: A short line at Crissy Field. Ways 1-3 are on or beside it; way 9 is ~1.6 km south and
#: is the negative control - a freeze that dumped whole tables would pass every count
#: assertion and still be wrong.
WAYS: list[dict[str, Any]] = [
    {
        "way_id": 1,
        "geometry": LineString([(-122.4650, 37.8050), (-122.4600, 37.8050)]),
        "tags": {"highway": "footway", "surface": "asphalt"},
    },
    {
        "way_id": 2,
        "geometry": LineString([(-122.4600, 37.8050), (-122.4550, 37.8051)]),
        "tags": {"highway": "primary", "maxspeed": "80", "lanes": "6", "sidewalk": "no"},
    },
    {
        "way_id": 3,
        "geometry": LineString([(-122.4640, 37.8054), (-122.4590, 37.8054)]),
        "tags": {"highway": "residential", "maxspeed": "25 mph", "sidewalk": "both"},
    },
    {
        "way_id": 9,
        "geometry": LineString([(-122.4650, 37.7900), (-122.4600, 37.7900)]),
        "tags": {"highway": "motorway"},
    },
]

NODES: list[dict[str, Any]] = [
    {"node_id": 11, "kind": "traffic_signals", "geometry": Point(-122.4600, 37.8050)},
    {"node_id": 19, "kind": "traffic_signals", "geometry": Point(-122.4600, 37.7900)},
]

AMENITIES: list[dict[str, Any]] = [
    {"node_id": 21, "kind": "drinking_water", "geometry": Point(-122.4620, 37.8052)},
    {"node_id": 29, "kind": "drinking_water", "geometry": Point(-122.4620, 37.7902)},
]

PARKS: list[dict[str, Any]] = [
    {
        "unit_id": 31,
        "name": "Crissy Field",
        "geometry": Polygon(
            [
                (-122.4645, 37.8045),
                (-122.4595, 37.8045),
                (-122.4595, 37.8058),
                (-122.4645, 37.8058),
            ]
        ),
    }
]

FROZEN_LAYERS = ("ways", "nodes", "amenities", "parks")

#: Layer name -> table name, derived from the store's own mapping rather than assumed.
#: They are not the same word: the `parks` layer lives in PAD-US's `units` table, so a
#: test that created a table called `parks` would be testing a mapping nothing uses.
TABLE = {layer: DEFAULT_LAYER_TABLES[layer].partition(".")[2] for layer in FROZEN_LAYERS}


@pytest.fixture(scope="module")
def route() -> Route:
    lons = [-122.4650 + i * 0.0005 for i in range(20)]
    return Route(
        id="freeze",
        name="Crissy Field",
        points=[
            RoutePoint(lat=37.8050, lon=lon, cum_dist_m=i * 44.0) for i, lon in enumerate(lons)
        ],
    )


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    try:
        connection = psycopg.connect(dsn_from_env(), connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no PostGIS at {dsn_from_env()}: {exc}")
    with connection:
        yield connection


@pytest.fixture(scope="module")
def loaded(conn: Any) -> Iterator[dict[str, str]]:
    """Load the records into a throwaway schema; yield the layer -> table mapping."""
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        cur.execute(
            f"CREATE TABLE {TEST_SCHEMA}.ways "
            "(way_id bigint PRIMARY KEY, tags jsonb, geom geometry(LineString, 4326))"
        )
        for way in WAYS:
            cur.execute(
                f"INSERT INTO {TEST_SCHEMA}.ways VALUES (%s, %s::jsonb, ST_GeomFromText(%s, 4326))",
                (way["way_id"], json.dumps(way["tags"]), way["geometry"].wkt),
            )
        for table, records in (("nodes", NODES), ("amenities", AMENITIES)):
            cur.execute(
                f"CREATE TABLE {TEST_SCHEMA}.{table} "
                "(node_id bigint PRIMARY KEY, kind text, geom geometry(Point, 4326))"
            )
            for record in records:
                cur.execute(
                    f"INSERT INTO {TEST_SCHEMA}.{table} VALUES (%s, %s, ST_GeomFromText(%s, 4326))",
                    (record["node_id"], record["kind"], record["geometry"].wkt),
                )
        cur.execute(
            f"CREATE TABLE {TEST_SCHEMA}.{TABLE['parks']} "
            "(unit_id bigint PRIMARY KEY, name text, geom geometry(Polygon, 4326))"
        )
        for park in PARKS:
            cur.execute(
                f"INSERT INTO {TEST_SCHEMA}.{TABLE['parks']} "
                "VALUES (%s, %s, ST_GeomFromText(%s, 4326))",
                (park["unit_id"], park["name"], park["geometry"].wkt),
            )
        cur.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, region, vintage) "
            "VALUES (%s, '_pytest_freeze', '', '2026-09-09') "
            "ON CONFLICT (layer_schema, source, region) DO UPDATE SET vintage = EXCLUDED.vintage",
            (TEST_SCHEMA,),
        )
        for table in TABLE.values():
            cur.execute(f"CREATE INDEX ON {TEST_SCHEMA}.{table} USING GIST (geom)")
    conn.commit()

    yield {layer: f"{TEST_SCHEMA}.{table}" for layer, table in TABLE.items()}

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        cur.execute("DELETE FROM meta.layer_vintage WHERE source = '_pytest_freeze'")
    conn.commit()


@pytest.fixture(scope="module")
def frozen(loaded: dict[str, str], route: Route, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the real command, once, and hand back the directory it wrote."""
    root = tmp_path_factory.mktemp("freeze")
    gpx_write(route, root / "route.gpx")
    out = root / "routes" / "crissy" / "fixtures"

    result = CliRunner().invoke(
        app,
        [
            "freeze-fixture",
            str(root / "route.gpx"),
            "--out",
            str(out),
            "--schema",
            TEST_SCHEMA,
            "--layers",
            ",".join(FROZEN_LAYERS),
        ],
    )
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    return out


@pytest.fixture(scope="module")
def postgis_store(conn: Any, loaded: dict[str, str]) -> PostGISLayerStore:
    return PostGISLayerStore(conn, tables=loaded)


@pytest.fixture(scope="module")
def file_store(frozen: Path) -> FileLayerStore:
    return FileLayerStore(frozen)


def _ids(frame: Any, column: str) -> set[int]:
    return set() if len(frame) == 0 else {int(v) for v in frame[column]}


# --- the round trip ---------------------------------------------------------


def test_every_requested_layer_was_written(frozen: Path) -> None:
    written = sorted(p.name for p in frozen.glob("*.gpkg"))
    assert written == sorted(f"{layer}.gpkg" for layer in FROZEN_LAYERS)


def test_the_frozen_ways_are_the_ways_postgis_returned(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    window = corridor(route)
    from_db = _ids(postgis_store.ways_in_corridor(window), "way_id")
    from_frozen = _ids(file_store.ways_in_corridor(window), "way_id")

    assert from_frozen == from_db
    # Both halves, or a freeze that wrote nothing would agree with a query that found
    # nothing and the test would prove only that two bugs cancel.
    assert {1, 2, 3} <= from_frozen
    assert 9 not in from_frozen


def test_the_tags_survive_the_freeze(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """Flattened `jsonb` reaches the GeoPackage as the columns a scorer reads."""
    from longrun.core.scorers.hostility import frame_tags_by_way

    window = corridor(route)
    from_db = frame_tags_by_way(postgis_store.ways_in_corridor(window))
    from_frozen = frame_tags_by_way(file_store.ways_in_corridor(window))

    for way_id, tags in from_db.items():
        for key, value in tags.items():
            assert str(from_frozen[way_id].get(key)) == str(value), f"way {way_id}, tag {key}"


def test_a_scorer_reads_the_same_lts_from_the_frozen_fixture(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """The contract that matters: the same conclusion, not merely the same rows.

    Way 2 is a six-lane 80 kph primary with no sidewalk. If it does not read LTS 4 from
    the frozen fixture, the freeze has lost something a scorer depends on, however well
    the row counts match.
    """
    from longrun.core.routing.lts import lts_from_tags
    from longrun.core.scorers.hostility import frame_tags_by_way

    window = corridor(route)
    db_tags = frame_tags_by_way(postgis_store.ways_in_corridor(window))
    frozen_tags = frame_tags_by_way(file_store.ways_in_corridor(window))

    assert lts_from_tags(frozen_tags[2]).level == 4
    for way_id in db_tags:
        assert lts_from_tags(frozen_tags[way_id]).level == lts_from_tags(db_tags[way_id]).level


def test_points_and_polygons_survive_with_their_negative_controls(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    window = corridor(route)
    for layer, column in (("nodes", "node_id"), ("amenities", "node_id")):
        from_db = _ids(postgis_store.points_in_corridor(window, [], layer=layer), column)
        from_frozen = _ids(file_store.points_in_corridor(window, [], layer=layer), column)
        assert from_frozen == from_db
        # Named explicitly: 19 is the far-away signal, 29 the far-away fountain. A range
        # test would have quietly excluded the on-route fountain at 21 as well.
        assert from_frozen
        assert 19 not in from_frozen
        assert 29 not in from_frozen

    parks_db = _ids(postgis_store.polygons_intersecting(window, "parks"), "unit_id")
    parks_frozen = _ids(file_store.polygons_intersecting(window, "parks"), "unit_id")
    assert parks_frozen == parks_db == {31}


def test_kind_filtering_still_works_on_the_frozen_layer(
    file_store: FileLayerStore, route: Route
) -> None:
    """The freeze takes every kind, so a scorer's filter has something to filter."""
    window = corridor(route)
    assert len(file_store.points_in_corridor(window, ["drinking_water"], layer="amenities")) == 1
    assert len(file_store.points_in_corridor(window, ["fuel"], layer="amenities")) == 0


def test_the_frozen_file_holds_the_corridor_and_not_the_table(frozen: Path) -> None:
    """What is *in the file*, not what a query over it returns.

    Every other assertion here compares the two stores, and `FileLayerStore` clips to the
    corridor on read — so a freeze that dumped whole tables would answer every corridor
    query correctly and still write a fixture the size of the region. Nothing above can
    see that, which is the whole reason this test reads the GeoPackage directly.
    """
    import geopandas as gpd

    ways = gpd.read_file(frozen / "ways.gpkg")
    assert set(ways["way_id"]) == {1, 2, 3}, "the far-south way was frozen into the fixture"

    for layer in ("nodes", "amenities"):
        frame = gpd.read_file(frozen / f"{layer}.gpkg")
        assert 19 not in set(frame["node_id"])
        assert 29 not in set(frame["node_id"])


def test_the_snapshot_records_a_vintage_for_every_frozen_layer(frozen: Path) -> None:
    """Scope 6.4: what the fixture is, is recorded next to the fixture."""
    pins = json.loads((frozen.parent / "snapshot.json").read_text(encoding="utf-8"))
    assert pins["layer_vintages"] == dict.fromkeys(FROZEN_LAYERS, "2026-09-09")


def test_a_layer_with_no_table_is_skipped_not_written_empty(
    loaded: dict[str, str], route: Route, tmp_path: Path
) -> None:
    """An empty GeoPackage would read as 'the corridor contains none', which is a lie.

    Scope 3.6 needs "nobody loaded this layer" and "this corridor has none of it" to stay
    apart, and `FileLayerStore` draws that line by whether the file exists at all.
    """
    gpx_write(route, tmp_path / "route.gpx")
    out = tmp_path / "fixtures"
    result = CliRunner().invoke(
        app,
        [
            "freeze-fixture",
            str(tmp_path / "route.gpx"),
            "--out",
            str(out),
            "--schema",
            TEST_SCHEMA,
            "--layers",
            "ways,flowlines",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipped flowlines" in result.output
    assert (out / "ways.gpkg").exists()
    assert not (out / "flowlines.gpkg").exists()
    assert not FileLayerStore(out).has_layer("flowlines")
