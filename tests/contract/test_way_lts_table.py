"""`osm_lts.way_lts` against the live database (scope 7.1, 13; ADR 0025).

The unit suite pins what a row should say. These pin the things only a real database can be
wrong about: that the table agrees with the scorer on ways nobody hand-wrote, that a re-run
costs nothing, and — the one that motivated the schema choice — that writing the LTS vintage
does not change the vintage every plan reports for `ways`.

    docker compose -f deploy/docker-compose.yml up -d
    uv run longrun build-region deploy/regions/ozarks.yaml
    uv run pytest tests/contract/test_way_lts_table.py -m network

`network`-marked because CI has no services. The Ozarks is the region they run against: 1,600
ways, so a full cycle is a tenth of a second, and small enough that a coverage regression is
visible — the same regression in the Bay Area still leaves 900,000 tagged ways and looks fine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from longrun.core.routing.lts import LTS_VERSION, lts_from_tags
from longrun.regions.lts import SCHEMA, TABLE, composite_vintage, lookup, resolve_aadt

pytestmark = pytest.mark.network

DSN = os.environ.get("LONGRUN_POSTGIS_DSN", "postgresql://longrun:longrun@localhost:5432/longrun")
REGION = Path("deploy/regions/ozarks.yaml")


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
def polygon() -> str:
    from longrun.regions.build import RegionSpec

    if not REGION.exists():
        pytest.skip(f"no region spec at {REGION}")
    return str(RegionSpec.load(REGION).shape().wkt)


@pytest.fixture(scope="module")
def scored(conn: Any, polygon: str) -> list[tuple[int, int, float, list[str], dict[str, Any]]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT to_regclass('{SCHEMA}.{TABLE}')")
        row = cur.fetchone()
        if not (row and row[0]):
            pytest.skip(f"{SCHEMA}.{TABLE} does not exist; run build-region first")
        cur.execute(
            f"SELECT l.way_id, l.lts, l.confidence, l.reasons, w.tags "
            f"FROM {SCHEMA}.{TABLE} l JOIN osm.ways w USING (way_id) "
            "WHERE ST_Intersects(w.geom, ST_GeomFromText(%s, 4326))",
            (polygon,),
        )
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no scored ways in the Ozarks; run build-region first")
    return rows


def test_the_stored_level_is_the_level_the_scorer_gives(scored: list[Any]) -> None:
    """The milestone's whole claim, checked against ways nobody wrote by hand.

    Until M9 the importer scored with its own tag-only approximation and the scorers with
    `lts_from_tags`; on this region the two disagreed about 469 of 1,600 ways. There is one
    implementation now, and this is what says so.
    """
    disagreements = [
        (way_id, stored, lts_from_tags(tags, aadt=resolve_aadt(tags, None)[0]).level)
        for way_id, stored, _confidence, _reasons, tags in scored
        if lts_from_tags(tags, aadt=resolve_aadt(tags, None)[0]).level != stored
    ]
    assert not disagreements, f"{len(disagreements)} way(s) differ, e.g. {disagreements[:3]}"


def test_every_scored_way_is_a_road(scored: list[Any]) -> None:
    """A railway tagged `lts=2` would sit in the graph at residential stress."""
    assert all(tags.get("highway") for *_rest, tags in scored)


def test_levels_are_in_range_and_spread(scored: list[Any]) -> None:
    levels = {stored for _id, stored, *_rest in scored}
    assert levels <= {1, 2, 3, 4}
    # A graph scored entirely one level is the failure mode that looks like success.
    assert len(levels) > 1, f"every way came out {levels}, which is what a broken lookup does"


def test_the_reasons_survive_the_round_trip(scored: list[Any]) -> None:
    """`text[]` rather than a joined string, so a disagreement is diagnosable in SQL."""
    for _id, _lts, _confidence, reasons, tags in scored[:50]:
        assert isinstance(reasons, list)
        assert reasons
        if tags.get("highway") in {"footway", "path", "cycleway"}:
            assert "separated_path" in reasons


def test_a_second_run_scores_nothing(conn: Any, polygon: str) -> None:
    """Incremental by `lts_version`, which is what makes a five-region build cheap.

    The extract vintage is read back and handed straight in again, so this rewrites the region's
    vintage row with the value it already had. Passing a literal here instead would leave the
    region pinned to whatever the test typed — which is what the first draft of this test did.
    """
    from longrun.regions import lts as lts_table

    with conn.cursor() as cur:
        cur.execute(
            "SELECT vintage FROM meta.layer_vintage WHERE layer_schema = %s AND region = %s",
            (SCHEMA, "ozarks"),
        )
        row = cur.fetchone()
    assert row is not None, f"no vintage row under {SCHEMA}; run build-region first"

    report = lts_table.compute(
        conn,
        region="ozarks",
        extract_vintage=row[0].split("+lts")[0],
        region_wkt=polygon,
        log=lambda _: None,
    )
    assert report.scored == 0, "a way already carrying a row at this version was re-scored"
    assert report.vintage == row[0], "the round trip changed the region's vintage"


def test_the_lts_vintage_does_not_become_the_vintage_of_ways(conn: Any) -> None:
    """The reason `way_lts` has its own schema, asserted rather than trusted.

    `PostGISLayerStore.vintage()` selects `WHERE layer_schema = %s ORDER BY loaded_at DESC` —
    schema only, no table and no source. A vintage row for the LTS computation written under
    `osm` would therefore be returned as the vintage for `ways`, `nodes` *and* `amenities`,
    because it is the newest row in that schema. Three layers made wrong by one row in the
    wrong place, and nothing else in the suite would notice.
    """
    from longrun.core.data.postgis import PostGISLayerStore

    store = PostGISLayerStore(conn)
    ways_vintage = store.vintage("ways")
    assert ways_vintage is not None
    assert "+lts" not in ways_vintage, f"the LTS vintage leaked into ways: {ways_vintage}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT vintage FROM meta.layer_vintage WHERE layer_schema = %s AND region = %s",
            (SCHEMA, "ozarks"),
        )
        row = cur.fetchone()
    assert row is not None, f"no vintage row under {SCHEMA}"
    assert row[0].endswith(f"+lts{LTS_VERSION}")
    assert row[0] == composite_vintage(row[0].split("+lts")[0])


def test_the_lookup_the_importer_uses_covers_the_scored_ways(
    conn: Any, polygon: str, scored: list[Any]
) -> None:
    """`add_lts_tags.py` reads this; an empty answer would tag nothing and say nothing."""
    table = lookup(conn, polygon_wkt=polygon)
    assert len(table) == len(scored)
    way_id, stored, *_rest = scored[0]
    assert table[way_id] == stored
