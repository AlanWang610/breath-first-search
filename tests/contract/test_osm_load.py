"""The OSM loader writes what `PostGISLayerStore` reads back (scope 13, ADR 0009, 0010).

The seam this milestone turns on has two halves written months apart: `PostGISLayerStore`
has named `osm.ways` and its siblings since M1.4, and nothing created them until now. A
test of either half alone proves nothing about the join — the store answers `LayerNotFound`
for a missing table, which is indistinguishable from a table the loader filled with the
wrong column names.

So this loads a hand-built extract and reads it back **through the store**, with the same
corridor query a scorer makes. `network`-marked because it needs the `ingest` extra and a
live database; CI has neither, by design.

    docker compose -f deploy/docker-compose.yml up -d
    uv run pytest tests/contract/test_osm_load.py -m network

A scratch schema, dropped afterwards, so running this never touches a loaded region.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.network

DSN = os.environ.get("LONGRUN_POSTGIS_DSN", "postgresql://longrun:longrun@localhost:5432/longrun")

SCHEMA = "osm_contract_scratch"

#: A short east-west street at a known place, with a second street crossing it, a signal
#: at the junction, a drinking fountain beside it, a supermarket mapped as a building
#: outline, and a bare railway line. Every one of those is a different code path.
LAT = 37.7900
LON = -122.4000
STEP = 0.001


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("osmium")
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no PostGIS at {DSN}: {exc}")
    with connection:
        yield connection
        with connection.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute("DELETE FROM meta.layer_vintage WHERE region = 'contract-scratch'")
        connection.commit()


@pytest.fixture(scope="module")
def extract(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A hand-built `.pbf`, so every expected row is known by construction."""
    import osmium
    from osmium.osm.mutable import Node, Way

    path = tmp_path_factory.mktemp("osm") / "contract.osm.pbf"
    writer = osmium.SimpleWriter(str(path))

    # The street grid: nodes 1-3 west to east, nodes 4-5 south to north through node 2.
    for index in range(3):
        writer.add_node(Node(id=index + 1, location=(LON + index * STEP, LAT), tags={}))
    writer.add_node(Node(id=4, location=(LON + STEP, LAT - STEP), tags={}))
    writer.add_node(Node(id=5, location=(LON + STEP, LAT + STEP), tags={}))

    # The signal sits on node 2, which is also a way node - a real OSM shape, and the one
    # that would break a loader that assumed control nodes are standalone.
    writer.add_node(Node(id=10, location=(LON + STEP, LAT), tags={"highway": "traffic_signals"}))
    writer.add_node(
        Node(
            id=11,
            location=(LON + 0.0005, LAT + 0.0002),
            tags={"amenity": "drinking_water", "opening_hours": "24/7"},
        )
    )
    # A supermarket as a building outline, which is how most of them are mapped. A
    # nodes-only loader finds the fountain and misses this.
    for index, (dx, dy) in enumerate([(0.0, 0.0), (0.0003, 0.0), (0.0003, 0.0003), (0.0, 0.0003)]):
        writer.add_node(
            Node(id=20 + index, location=(LON + 0.0015 + dx, LAT + 0.0005 + dy), tags={})
        )
    writer.add_way(
        Way(id=200, nodes=[20, 21, 22, 23, 20], tags={"shop": "supermarket", "name": "Market"})
    )

    writer.add_way(
        Way(id=100, nodes=[1, 2, 3], tags={"highway": "residential", "surface": "asphalt"})
    )
    writer.add_way(Way(id=101, nodes=[4, 5], tags={"highway": "primary", "maxspeed": "35 mph"}))
    # A bare rail line and a tram in the street: only the first is a `railways` row.
    writer.add_way(Way(id=102, nodes=[1, 3], tags={"railway": "rail"}))
    writer.add_way(Way(id=103, nodes=[4, 5], tags={"railway": "tram", "highway": "secondary"}))
    # No routing tag at all: must not be loaded anywhere.
    writer.add_way(Way(id=104, nodes=[20, 21, 22, 23, 20], tags={"building": "yes"}))
    writer.close()
    return path


@pytest.fixture(scope="module")
def loaded(conn: Any, extract: Path) -> Any:
    from longrun.core.data.osm import load_extract

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    return load_extract(extract, conn, region="contract-scratch", schema=SCHEMA)


def _store(conn: Any) -> Any:
    from longrun.core.data.postgis import DEFAULT_LAYER_TABLES, PostGISLayerStore

    tables = {
        name: f"{SCHEMA}.{table.partition('.')[2]}"
        for name, table in DEFAULT_LAYER_TABLES.items()
        if table.startswith("osm.")
    }
    return PostGISLayerStore(conn, tables=tables)


def _route() -> Any:
    """A route along the east-west street, as `Route` rather than as raw coordinates."""
    from longrun.core.models.geometry import Route, RoutePoint

    points = [
        RoutePoint(lat=LAT, lon=LON + index * STEP, cum_dist_m=index * 88.0) for index in range(3)
    ]
    return Route(id="contract", points=points)


# --- what was loaded --------------------------------------------------------


def test_the_load_reports_what_it_wrote(loaded: Any) -> None:
    """Two roads and a tram street in `ways`; the bare rail line separately."""
    assert loaded.counts["ways"] == 3, loaded.counts
    assert loaded.counts["railways"] == 1, loaded.counts
    assert loaded.counts["nodes"] == 1, loaded.counts
    assert loaded.counts["amenities"] == 2, loaded.counts
    assert loaded.vintage


def test_a_way_with_no_routing_tag_is_in_no_table(conn: Any, loaded: Any) -> None:
    """The building outline. A loader that keeps it inflates every corridor query."""
    with conn.cursor() as cur:
        for table in ("ways", "railways"):
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.{table} WHERE way_id = 104")
            assert cur.fetchone()[0] == 0, table


def test_a_shop_mapped_as_a_building_becomes_a_point(conn: Any, loaded: Any) -> None:
    """The failure a nodes-only loader has: it finds fountains and misses every shop.

    The id is negated, so the row is still traceable to way 200 and cannot collide with a
    node of the same id.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT kind, ST_X(geom), ST_Y(geom) FROM {SCHEMA}.amenities WHERE osm_id = -200"
        )
        row = cur.fetchone()
    assert row is not None, "the supermarket building did not become an amenity"
    kind, x, y = row
    assert kind == "supermarket"
    # The centroid of the square, not one of its corners.
    assert LON + 0.0015 < x < LON + 0.0018
    assert LAT + 0.0005 < y < LAT + 0.0008


# --- read back through the store, as a scorer would -------------------------


def test_a_corridor_query_returns_the_ways_with_their_tags_flat(conn: Any, loaded: Any) -> None:
    """The join the whole milestone is for: jsonb in, `row["highway"]` out."""
    from longrun.core.geo.segments import corridor

    frame = _store(conn).ways_in_corridor(corridor(_route(), buffer_m=200.0))
    assert len(frame) >= 2, f"expected the street and its cross street, got {len(frame)}"
    assert "highway" in frame.columns, list(frame.columns)
    by_way = {int(row["way_id"]): row for _, row in frame.iterrows()}
    assert by_way[100]["highway"] == "residential"
    assert by_way[100]["surface"] == "asphalt"


def test_the_kind_filter_a_scorer_sends_reaches_the_right_rows(conn: Any, loaded: Any) -> None:
    """`crossings` asks `osm.nodes` for signals and `services` asks `osm.amenities`.

    Both go through `points_in_corridor`, and a kind list that matches nothing comes back
    empty — which every scorer reads as "none here". This is what proves the vocabulary in
    `core.data.osm` and the one in the scorers meet.
    """
    from longrun.core.geo.segments import corridor
    from longrun.core.scorers.crossings import SIGNAL_KINDS
    from longrun.core.scorers.services import all_kinds

    store = _store(conn)
    box = corridor(_route(), buffer_m=200.0)

    signals = store.points_in_corridor(box, list(SIGNAL_KINDS), layer="nodes")
    assert len(signals) == 1, f"expected the one signal, got {len(signals)}"
    assert signals.iloc[0]["kind"] == "traffic_signals"

    services = store.points_in_corridor(box, all_kinds(), layer="amenities")
    kinds = {str(row["kind"]) for _, row in services.iterrows()}
    assert kinds == {"drinking_water", "supermarket"}, kinds


def test_a_rail_line_is_crossable_and_a_tram_street_is_not_a_rail_line(
    conn: Any, loaded: Any
) -> None:
    """`hazards` will ask `lines_crossing(route, "railways")`, and a tram in a street is
    not an at-grade rail hazard — `legality` draws the same line for the same reason."""
    from longrun.core.data.postgis import DEFAULT_LAYER_TABLES, PostGISLayerStore

    tables = dict(DEFAULT_LAYER_TABLES)
    tables["railways"] = f"{SCHEMA}.railways"
    store = PostGISLayerStore(conn, tables=tables)
    frame = store.lines_crossing(_route(), "railways")
    ids = {int(row["way_id"]) for _, row in frame.iterrows()}
    assert ids == {102}, ids


def test_the_vintage_reaches_the_manifest(conn: Any, loaded: Any) -> None:
    """Scope 6.4 pins every source. The store reads it back from `meta.layer_vintage`."""
    from longrun.core.data.postgis import PostGISLayerStore

    store = PostGISLayerStore(conn, tables={"ways": f"{SCHEMA}.ways"})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vintage, source, source_url FROM meta.layer_vintage "
            "WHERE layer_schema = %s AND region = %s",
            (SCHEMA, "contract-scratch"),
        )
        row = cur.fetchone()
    assert row is not None, "the load wrote no vintage row"
    assert row[1] == "openstreetmap"
    # `vintage()` keys on the schema, which for a scratch load is the scratch schema.
    assert store.vintage("ways") == row[0]


# --- idempotence ------------------------------------------------------------


def test_loading_the_same_extract_twice_changes_nothing(
    conn: Any, extract: Path, loaded: Any
) -> None:
    """Scope 13 calls the steps idempotent, and ADR 0010 makes that an upsert on the OSM id.

    The failure this catches is a duplicate-key error on the second run, which is what a
    plain `COPY` into the live table gives — and which would make a resumed build fail
    exactly where it was meant to resume.
    """
    from longrun.core.data.osm import load_extract

    def counts() -> dict[str, int]:
        with conn.cursor() as cur:
            out = {}
            for table in ("ways", "railways", "nodes", "amenities"):
                cur.execute(f"SELECT count(*) FROM {SCHEMA}.{table}")
                out[table] = cur.fetchone()[0]
            return out

    before = counts()
    load_extract(extract, conn, region="contract-scratch", schema=SCHEMA)
    assert counts() == before
