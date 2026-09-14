"""The deployed PostGIS schema matches what the code expects (scope 6.4, 13).

`deploy/postgis/init/01-schemas.sql` runs exactly once, against an empty data directory,
so nothing re-checks it afterwards: a container brought up months ago against an older
version of that file looks identical to a fresh one until a loader fails. These tests are
the check, and they are `network`-marked because they need the live database - CI has no
services, by design (the `LayerStore` seam is what makes that possible).

    docker compose -f deploy/docker-compose.yml up -d
    uv run pytest tests/contract/test_postgis_schema.py -m network

They assert the *contract* the loaders and `LayerStore.vintage()` depend on, not every
detail of the file, so a comment or an added column does not turn them red.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.network

DSN = os.environ.get("LONGRUN_POSTGIS_DSN", "postgresql://longrun:longrun@localhost:5432/longrun")

#: Every layer group the scope's regions are built from (scope 11, 13).
#:
#: `user_data`, not `user`: USER is reserved, so a schema named `user` needs quoting in
#: every statement forever, and an unquoted reference fails at parse time rather than
#: obviously.
EXPECTED_SCHEMAS = frozenset(
    {"osm", "hpms", "overture", "padus", "nhd", "tiger", "gtfs", "user_data", "meta"}
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


def _scalar(conn: Any, sql: str, *args: object) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, args or None)
        row = cur.fetchone()
        return None if row is None else row[0]


def test_postgis_and_raster_extensions_are_installed(conn: Any) -> None:
    """`postgis_raster` is separate in PostGIS 3 and easy to omit; DEM reads need it."""
    with conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension")
        installed = {name for (name,) in cur.fetchall()}
    assert {"postgis", "postgis_raster"} <= installed


def test_every_layer_schema_exists(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            r"SELECT nspname FROM pg_namespace "
            r"WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'"
        )
        present = {name for (name,) in cur.fetchall()}
    assert EXPECTED_SCHEMAS <= present, f"missing: {sorted(EXPECTED_SCHEMAS - present)}"


def test_the_reserved_word_schema_was_not_created(conn: Any) -> None:
    """A plain `user` schema would mean the init SQL is an older version of the file."""
    assert _scalar(conn, "SELECT count(*) FROM pg_namespace WHERE nspname = 'user'") == 0


def test_the_vintage_ledger_has_the_columns_the_manifest_reads(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'meta' AND table_name = 'layer_vintage'"
        )
        columns = {name for (name,) in cur.fetchall()}
    assert {"layer_schema", "source", "region", "vintage", "source_url", "loaded_at"} <= columns


def test_a_source_can_be_recorded_nationally_and_per_region(conn: Any) -> None:
    """Scope 6.4: the same source may be loaded once nationally and again per region.

    `region` is an empty string rather than NULL for the national case precisely so both
    rows can coexist under the primary key - a NULL there would not be comparable, and a
    primary key cannot hold one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, region, vintage) "
            "VALUES ('osm', '_pytest', '', 'v1'), ('osm', '_pytest', 'bayarea', 'v2')"
        )
        cur.execute(
            "SELECT count(*) FROM meta.layer_vintage WHERE source = '_pytest'",
        )
        assert cur.fetchone()[0] == 2
    conn.rollback()


def test_reloading_the_same_source_and_region_is_rejected(conn: Any) -> None:
    """The ledger records one vintage per (schema, source, region), not a growing log."""
    psycopg = pytest.importorskip("psycopg")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, vintage) "
            "VALUES ('osm', '_pytest_dup', 'v1')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO meta.layer_vintage (layer_schema, source, vintage) "
                "VALUES ('osm', '_pytest_dup', 'v2')"
            )
    conn.rollback()


def test_text_ordering_is_platform_stable(conn: Any) -> None:
    """C collation, so a query's row order does not depend on the host's locale.

    Fixtures are frozen from this database's answers, and ordering that shifts between a
    Linux container and a Windows host would make those fixtures unreproducible.
    """
    assert _scalar(
        conn, "SELECT datcollate FROM pg_database WHERE datname = current_database()"
    ) in {
        "C",
        "C.UTF-8",
    }


def test_a_corridor_buffer_is_metric_not_degrees(conn: Any) -> None:
    """The query shape every scorer runs (scope 5): buffer in local UTM, then intersect.

    Buffering in degrees is the classic error - 400 m of longitude is not 400 m of
    latitude - so this asserts a way 1.6 km north of the route is *excluded* by a 400 m
    corridor, which a degree-space buffer of 400 would swallow whole.
    """
    route = "LINESTRING(-122.3937 37.7955, -122.3900 37.7955)"
    alongside = "LINESTRING(-122.3930 37.7956, -122.3910 37.7956)"
    far_north = "LINESTRING(-122.3937 37.8100, -122.3900 37.8100)"

    query = """
        WITH corridor AS (
            SELECT ST_Transform(
                ST_Buffer(ST_Transform(ST_GeomFromText(%s, 4326), 32610), 400), 4326
            ) AS g
        )
        SELECT count(*) FROM corridor c
        WHERE ST_Intersects(ST_GeomFromText(%s, 4326), c.g)
    """

    # Both halves, because "no rows" on its own is what a broken query also returns.
    assert _scalar(conn, query, route, alongside) == 1, "a way on the route was excluded"
    assert _scalar(conn, query, route, far_north) == 0, "a way 1.6 km away was included"
