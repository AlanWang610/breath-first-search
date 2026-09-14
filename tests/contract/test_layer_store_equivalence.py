"""The two `LayerStore` implementations answer the same questions the same way.

This is the test the seam in `core.data.base` exists for. The whole suite runs on
`FileLayerStore`, which is what makes CI hermetic and fast — but that is only sound while
the file store is a faithful stand-in for the database the production path actually uses.
Without this test, "the golden suite passes" would say nothing about PostGIS.

Both stores are loaded from one set of records, so a disagreement is a *query* difference
rather than a data difference. The corridor geometry is shared code
(`core.geo.segments.corridor_polygon`), so it cannot be the explanation either.

`network`-marked: needs the live database.

    docker compose -f deploy/docker-compose.yml up -d
    uv run pytest tests/contract/test_layer_store_equivalence.py -m network
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from longrun.core.data.file_store import FileLayerStore, LayerNotFound
from longrun.core.data.postgis import PostGISLayerStore
from longrun.core.geo.segments import corridor
from longrun.core.models.geometry import Route, RoutePoint

pytestmark = pytest.mark.network

DSN = os.environ.get("LONGRUN_POSTGIS_DSN", "postgresql://longrun:longrun@localhost:5432/longrun")

#: Schema for the fixtures, kept out of the layer schemas so a failed run cannot leave
#: debris where a real region build would load.
TEST_SCHEMA = "pytest_equivalence"

#: A short Embarcadero line. Ways 1-3 lie on or beside it; way 9 is ~1.6 km north, outside
#: any sane corridor, and is the negative control - without it a query returning everything
#: would pass as readily as a correct one.
WAYS: list[dict[str, Any]] = [
    {
        "way_id": 1,
        "geometry": LineString([(-122.3937, 37.7955), (-122.3900, 37.7955)]),
        "tags": {"highway": "residential", "maxspeed": "25 mph", "sidewalk": "both"},
    },
    {
        "way_id": 2,
        "geometry": LineString([(-122.3900, 37.7955), (-122.3860, 37.7956)]),
        "tags": {"highway": "primary", "maxspeed": "80", "lanes": "6", "sidewalk": "no"},
    },
    {
        "way_id": 3,
        "geometry": LineString([(-122.3930, 37.7960), (-122.3890, 37.7960)]),
        "tags": {"highway": "footway", "surface": "asphalt"},
    },
    {
        "way_id": 9,
        "geometry": LineString([(-122.3937, 37.8100), (-122.3900, 37.8100)]),
        "tags": {"highway": "motorway"},
    },
]

NODES: list[dict[str, Any]] = [
    {"node_id": 11, "kind": "traffic_signals", "geometry": Point(-122.3900, 37.7955)},
    {"node_id": 12, "kind": "gate", "geometry": Point(-122.3890, 37.7956)},
    {"node_id": 19, "kind": "traffic_signals", "geometry": Point(-122.3900, 37.8100)},
]

AMENITIES: list[dict[str, Any]] = [
    {"node_id": 21, "kind": "drinking_water", "geometry": Point(-122.3910, 37.7957)},
    {"node_id": 22, "kind": "toilets", "geometry": Point(-122.3895, 37.7953)},
    {"node_id": 29, "kind": "drinking_water", "geometry": Point(-122.3910, 37.8102)},
]

PARKS: list[dict[str, Any]] = [
    {
        "unit_id": 31,
        "name": "Embarcadero Plaza",
        "geometry": Polygon(
            [
                (-122.3925, 37.7950),
                (-122.3905, 37.7950),
                (-122.3905, 37.7962),
                (-122.3925, 37.7962),
            ]
        ),
    },
    {
        "unit_id": 39,
        "name": "Far North Park",
        "geometry": Polygon(
            [
                (-122.3925, 37.8095),
                (-122.3905, 37.8095),
                (-122.3905, 37.8105),
                (-122.3925, 37.8105),
            ]
        ),
    },
]


@pytest.fixture(scope="module")
def route() -> Route:
    lons = [-122.3937 + i * 0.0004 for i in range(20)]
    return Route(
        id="equivalence",
        name="Embarcadero",
        points=[
            RoutePoint(lat=37.7955, lon=lon, cum_dist_m=i * 35.0) for i, lon in enumerate(lons)
        ],
    )


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no PostGIS at {DSN}: {exc}")
    with connection:
        yield connection


@pytest.fixture(scope="module")
def postgis_store(conn: Any) -> Iterator[PostGISLayerStore]:
    """Load the records into a throwaway schema and hand back a store over it."""
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
            f"CREATE TABLE {TEST_SCHEMA}.parks "
            "(unit_id bigint PRIMARY KEY, name text, geom geometry(Polygon, 4326))"
        )
        for park in PARKS:
            cur.execute(
                f"INSERT INTO {TEST_SCHEMA}.parks VALUES (%s, %s, ST_GeomFromText(%s, 4326))",
                (park["unit_id"], park["name"], park["geometry"].wkt),
            )

        # The index the loaders are meant to create, so the query plan under test is the
        # one production would run rather than a sequential scan that happens to agree.
        for table in ("ways", "nodes", "amenities", "parks"):
            cur.execute(f"CREATE INDEX ON {TEST_SCHEMA}.{table} USING GIST (geom)")
    conn.commit()

    tables = {name: f"{TEST_SCHEMA}.{name}" for name in ("ways", "nodes", "amenities", "parks")}
    yield PostGISLayerStore(conn, tables=tables)

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    conn.commit()


@pytest.fixture(scope="module")
def file_store(tmp_path_factory: pytest.TempPathFactory) -> FileLayerStore:
    """The same records, written to GeoPackages, with tags flattened as a fixture would."""
    root = tmp_path_factory.mktemp("layers")

    flattened = [{"way_id": w["way_id"], **w["tags"], "geometry": w["geometry"]} for w in WAYS]
    _write(root / "ways.gpkg", flattened)
    _write(root / "nodes.gpkg", NODES)
    _write(root / "amenities.gpkg", AMENITIES)
    _write(root / "parks.gpkg", PARKS)
    return FileLayerStore(root)


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    frame = gpd.GeoDataFrame(
        [{k: v for k, v in r.items() if k != "geometry"} for r in records],
        geometry=[r["geometry"] for r in records],
        crs="EPSG:4326",
    )
    frame.to_file(path, driver="GPKG")


def _ids(frame: Any, column: str) -> set[int]:
    return set() if len(frame) == 0 else {int(v) for v in frame[column]}


def _tags_by_way(frame: Any) -> dict[int, dict[str, Any]]:
    """What `hostility.frame_tags_by_way` will see, so the comparison is the real contract."""
    from longrun.core.scorers._common import frame_tags_by_way

    return frame_tags_by_way(frame)


# --- the equivalence itself -------------------------------------------------


def test_both_stores_satisfy_the_protocol(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore
) -> None:
    from longrun.core.data.base import LayerStore

    assert isinstance(postgis_store, LayerStore)
    assert isinstance(file_store, LayerStore)


def test_the_same_ways_are_in_the_corridor(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    window = corridor(route)
    from_db = _ids(postgis_store.ways_in_corridor(window), "way_id")
    from_file = _ids(file_store.ways_in_corridor(window), "way_id")

    assert from_db == from_file
    # Both halves: the corridor must include the on-route ways and exclude the distant one,
    # or two stores that both return nothing would agree perfectly and prove nothing.
    assert {1, 2, 3} <= from_db
    assert 9 not in from_db


def test_way_tags_survive_the_jsonb_round_trip(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """PostGIS keeps tags in one `jsonb`; a GeoPackage keeps them as columns.

    The scorers read columns, so the store flattens - and this asserts the flattening lands
    on exactly the tags the file store would have handed over.
    """
    window = corridor(route)
    from_db = _tags_by_way(postgis_store.ways_in_corridor(window))
    from_file = _tags_by_way(file_store.ways_in_corridor(window))

    assert from_db.keys() == from_file.keys()
    for way_id in from_db:
        expected = {k: v for k, v in from_file[way_id].items() if k != "way_id"}
        actual = {k: v for k, v in from_db[way_id].items() if k != "way_id"}
        assert actual == expected, f"way {way_id} tags differ"


def test_a_real_scorer_reaches_the_same_verdict_on_either_store(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """The end that matters: the same LTS levels, not merely the same rows.

    Way 2 is a 6-lane 80 kph primary with no sidewalk, so it must come out LTS 4 from both
    - a level, not just a matching row count.
    """
    from longrun.core.routing.lts import lts_from_tags

    window = corridor(route)
    db_tags = _tags_by_way(postgis_store.ways_in_corridor(window))
    file_tags = _tags_by_way(file_store.ways_in_corridor(window))

    db_levels = {w: lts_from_tags(t).level for w, t in db_tags.items()}
    file_levels = {w: lts_from_tags(t).level for w, t in file_tags.items()}

    assert db_levels == file_levels
    assert db_levels[2] == 4, "the hostile arterial must read as LTS 4 from the database"
    assert db_levels[1] < 4


def test_point_layers_are_kept_apart(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """Signals come from `nodes`, water from `amenities` (scope 3.6).

    Sharing one layer would make a region that extracted fountains but never extracted
    signal nodes look like a region with no signals - "unknown" reported as a confident
    "none".
    """
    window = corridor(route)
    for layer, kinds, expected in (
        ("nodes", ["traffic_signals", "gate"], {11, 12}),
        ("amenities", ["drinking_water", "toilets"], {21, 22}),
    ):
        from_db = _ids(postgis_store.points_in_corridor(window, kinds, layer=layer), "node_id")
        from_file = _ids(file_store.points_in_corridor(window, kinds, layer=layer), "node_id")
        assert from_db == from_file == expected


def test_kind_filtering_agrees(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    window = corridor(route)
    from_db = _ids(postgis_store.points_in_corridor(window, ["gate"], layer="nodes"), "node_id")
    from_file = _ids(file_store.points_in_corridor(window, ["gate"], layer="nodes"), "node_id")
    assert from_db == from_file == {12}


def test_polygon_layers_agree(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    window = corridor(route)
    from_db = _ids(postgis_store.polygons_intersecting(window, "parks"), "unit_id")
    from_file = _ids(file_store.polygons_intersecting(window, "parks"), "unit_id")
    assert from_db == from_file
    assert 31 in from_db and 39 not in from_db


def test_lines_crossing_uses_the_route_not_the_corridor(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """Way 3 is 50 m off the line: inside the corridor, not crossed by the route."""
    from_db = _ids(postgis_store.lines_crossing(route, "ways"), "way_id")
    from_file = _ids(file_store.lines_crossing(route, "ways"), "way_id")
    assert from_db == from_file
    assert 3 not in from_db, "a way beside the route is not a way the route crosses"


def test_a_missing_layer_raises_rather_than_returning_empty(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """Scope 3.6: "never loaded" must not be indistinguishable from "none found"."""
    window = corridor(route)
    with pytest.raises(LayerNotFound):
        postgis_store.polygons_intersecting(window, "flowlines")
    with pytest.raises(LayerNotFound):
        file_store.polygons_intersecting(window, "flowlines")


def test_an_empty_result_is_a_frame_not_an_error(
    postgis_store: PostGISLayerStore, file_store: FileLayerStore, route: Route
) -> None:
    """The other half of the same distinction: a real layer with no hits is evidence."""
    window = corridor(route)
    assert len(postgis_store.points_in_corridor(window, ["fuel"], layer="amenities")) == 0
    assert len(file_store.points_in_corridor(window, ["fuel"], layer="amenities")) == 0


def test_vintage_prefers_a_region_row_over_a_national_one(
    conn: Any, postgis_store: PostGISLayerStore
) -> None:
    """Scope 6.4: the manifest pins what the database holds for *this* region."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, region, vintage) VALUES "
            "(%s, '_pytest', '', 'national-2026-01-01'), "
            "(%s, '_pytest', 'bayarea', 'region-2026-09-01')",
            (TEST_SCHEMA, TEST_SCHEMA),
        )
        assert postgis_store.vintage("ways") == "region-2026-09-01"
    conn.rollback()
