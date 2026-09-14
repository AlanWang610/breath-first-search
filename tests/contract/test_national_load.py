"""National sources reach PostGIS in the shape the store reads (scope 13 step 2).

Deliberately does **not** download. The fetch is 100 MB of TIGER and 121 MB of NHD, and a
test that spends four minutes on federal endpoints is a test people stop running. What
needs a live database is the *write*: whether `load_frame`'s DDL, its typed geometry column
and its upsert produce a table `PostGISLayerStore` can answer a corridor query from.

So the frames here are hand-built with the columns the normalisers emit, and the real
normalisers are exercised on real files by `test_national.py` in CI. Between them the whole
path is covered and neither half needs the other's cost.

    docker compose -f deploy/docker-compose.yml up -d
    uv run pytest tests/contract/test_national_load.py -m network
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from longrun.core.data.national import NHD, TIGER, NationalSource, load_frame

pytestmark = pytest.mark.network

DSN = os.environ.get("LONGRUN_POSTGIS_DSN", "postgresql://longrun:longrun@localhost:5432/longrun")

SCHEMA = "national_contract_scratch"
REGION = "contract-scratch"

LAT = 37.7900
LON = -122.4000


def _scratch(spec: NationalSource) -> NationalSource:
    """The same source pointed at a throwaway schema, so a run never touches a real load."""
    from dataclasses import replace

    return replace(spec, schema=SCHEMA)


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no PostGIS at {DSN}: {exc}")
    with connection:
        with connection.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        connection.commit()
        yield connection
        with connection.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute("DELETE FROM meta.layer_vintage WHERE region = %s", (REGION,))
        connection.commit()


def _boundaries() -> gpd.GeoDataFrame:
    """A county containing the route and one that does not, so the query can be wrong."""
    return gpd.GeoDataFrame(
        [
            {"geoid": "06075", "level": "county", "name": "Here", "statefp": "06", "lsad": "06"},
            {
                "geoid": "06001",
                "level": "county",
                "name": "Elsewhere",
                "statefp": "06",
                "lsad": "06",
            },
        ],
        geometry=[
            Polygon(
                [
                    (LON - 0.02, LAT - 0.02),
                    (LON + 0.02, LAT - 0.02),
                    (LON + 0.02, LAT + 0.02),
                    (LON - 0.02, LAT + 0.02),
                ]
            ),
            Polygon([(LON + 1, LAT), (LON + 1.1, LAT), (LON + 1.1, LAT + 0.1), (LON + 1, LAT)]),
        ],
        crs="EPSG:4326",
    )


def _flowlines() -> gpd.GeoDataFrame:
    """One creek across the route line, one alongside it, and a 3D one.

    The third is the case that actually broke: NHD publishes measured 3D linestrings and
    the column is 2D, so all 110,982 Bay Area flowlines were rejected by `COPY` until
    `_as_multi` flattened them.
    """
    return gpd.GeoDataFrame(
        [
            {
                "permanent_identifier": "across",
                "gnis_name": "Cross Creek",
                "ftype": 460,
                "fcode": 46006,
                "huc4": "1805",
            },
            {
                "permanent_identifier": "alongside",
                "gnis_name": "Parallel Creek",
                "ftype": 460,
                "fcode": 46006,
                "huc4": "1805",
            },
            {
                "permanent_identifier": "raised",
                "gnis_name": "Three D Creek",
                "ftype": 460,
                "fcode": 46006,
                "huc4": "1805",
            },
        ],
        geometry=[
            LineString([(LON + 0.001, LAT - 0.002), (LON + 0.001, LAT + 0.002)]),
            LineString([(LON - 0.01, LAT + 0.01), (LON + 0.01, LAT + 0.01)]),
            LineString([(LON + 0.0015, LAT - 0.002, 12.0), (LON + 0.0015, LAT + 0.002, 14.0)]),
        ],
        crs="EPSG:4326",
    )


def _route() -> Any:
    from longrun.core.models.geometry import Route, RoutePoint

    return Route(
        id="contract",
        points=[RoutePoint(lat=LAT, lon=LON + i * 0.001, cum_dist_m=i * 88.0) for i in range(3)],
    )


def _store(conn: Any, layer: str, spec: NationalSource) -> Any:
    from longrun.core.data.postgis import PostGISLayerStore

    return PostGISLayerStore(conn, tables={layer: _scratch(spec).qualified})


# --- the write path ---------------------------------------------------------


def test_a_frame_loads_and_reports_what_it_wrote(conn: Any) -> None:
    written = load_frame(conn, _boundaries(), _scratch(TIGER), region=REGION)
    assert written == 2


def test_a_three_dimensional_geometry_survives_the_typed_column(conn: Any) -> None:
    """The failure that stopped the real NHD load: every flowline rejected by `COPY`."""
    assert load_frame(conn, _flowlines(), _scratch(NHD), region=REGION) == 3
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.flowlines WHERE ST_NDims(geom) <> 2")
        assert cur.fetchone()[0] == 0


def test_a_corridor_query_finds_the_containing_jurisdiction_and_not_the_other(conn: Any) -> None:
    """Scope 13 step 4 in one query: which jurisdictions does this corridor cross."""
    from longrun.core.geo.segments import corridor

    load_frame(conn, _boundaries(), _scratch(TIGER), region=REGION)
    frame = _store(conn, "boundaries", TIGER).polygons_intersecting(
        corridor(_route(), buffer_m=200.0), "boundaries"
    )
    names = {str(row["name"]) for _, row in frame.iterrows()}
    assert names == {"Here"}, names


def test_only_the_flowline_that_crosses_the_route_is_returned(conn: Any) -> None:
    """`hazards` asks `lines_crossing`, so a creek running alongside must not answer."""
    load_frame(conn, _flowlines(), _scratch(NHD), region=REGION)
    frame = _store(conn, "flowlines", NHD).lines_crossing(_route(), "flowlines")
    ids = {str(row["permanent_identifier"]) for _, row in frame.iterrows()}
    assert ids == {"across", "raised"}, ids


def test_loading_twice_changes_nothing(conn: Any) -> None:
    """Scope 13 calls the steps idempotent. A source staged per watershed or per state
    repeats the features that span the boundary, so this is the common case, not the edge."""
    load_frame(conn, _boundaries(), _scratch(TIGER), region=REGION)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.boundaries")
        before = cur.fetchone()[0]
    load_frame(conn, _boundaries(), _scratch(TIGER), region=REGION)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.boundaries")
        assert cur.fetchone()[0] == before


def test_a_duplicate_id_within_one_frame_does_not_abort_the_load(conn: Any) -> None:
    """Postgres refuses to update one row twice in a statement rather than picking a winner,
    and a HUC4 boundary duplicates the flowlines that cross it. `DISTINCT ON` is the fix and
    this is what would catch its removal."""
    doubled = gpd.GeoDataFrame(
        [
            {"geoid": "06075", "level": "county", "name": "First", "statefp": "06", "lsad": "06"},
            {"geoid": "06075", "level": "county", "name": "Second", "statefp": "06", "lsad": "06"},
        ],
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])] * 2,
        crs="EPSG:4326",
    )
    assert load_frame(conn, doubled, _scratch(TIGER), region=REGION) == 2


def test_a_geometry_of_the_wrong_family_is_dropped_rather_than_aborting(conn: Any) -> None:
    """One bad row in a federal extract must not cost the other hundred thousand."""
    mixed = gpd.GeoDataFrame(
        [
            {
                "permanent_identifier": "good",
                "gnis_name": "Fine",
                "ftype": 460,
                "fcode": 1,
                "huc4": "1805",
            },
            {
                "permanent_identifier": "bad",
                "gnis_name": "Point",
                "ftype": 460,
                "fcode": 1,
                "huc4": "1805",
            },
        ],
        geometry=[LineString([(0, 0), (1, 1)]), Point(0, 0)],
        crs="EPSG:4326",
    )
    assert load_frame(conn, mixed, _scratch(NHD), region=REGION) == 1


def test_the_vintage_reaches_the_manifest(conn: Any) -> None:
    """Scope 6.4 pins every source, and the store reads it back by schema."""
    load_frame(conn, _boundaries(), _scratch(TIGER), region=REGION, source_url="https://example")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vintage, source, source_url FROM meta.layer_vintage "
            "WHERE layer_schema = %s AND region = %s",
            (SCHEMA, REGION),
        )
        rows = {r[1]: r for r in cur.fetchall()}
    assert rows["tiger"][0] == TIGER.vintage
    assert rows["tiger"][2] == "https://example"
