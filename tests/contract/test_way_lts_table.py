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

from longrun.core.routing.lts import LTS_VERSION, lts_from_tags, survey_speed_of
from longrun.regions.lts import (
    SCHEMA,
    SURVEY_TABLE,
    TABLE,
    composite_vintage,
    lookup,
    resolve_aadt,
    with_survey,
)

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
        # The surveyed speed the row was scored with, merged back under the plan-time
        # column's name (M18) - `None` on every way of a region with no survey, which is
        # every region until a speed-survey adapter exists.
        cur.execute(
            f"SELECT l.way_id, l.lts, l.confidence, l.reasons, w.tags, l.operating_speed_kph "
            f"FROM {SCHEMA}.{TABLE} l JOIN osm.ways w USING (way_id) "
            "WHERE ST_Intersects(w.geom, ST_GeomFromText(%s, 4326))",
            (polygon,),
        )
        rows = [
            (way_id, lts, confidence, reasons, with_survey(tags, speed, "survey"))
            for way_id, lts, confidence, reasons, tags, speed in cur.fetchall()
        ]
    if not rows:
        pytest.skip("no scored ways in the Ozarks; run build-region first")
    return rows


def test_the_stored_level_is_the_level_the_scorer_gives(scored: list[Any]) -> None:
    """The milestone's whole claim, checked against ways nobody wrote by hand.

    Until M9 the importer scored with its own tag-only approximation and the scorers with
    `lts_from_tags`; on this region the two disagreed about 469 of 1,600 ways. There is one
    implementation now, and this is what says so.
    """

    def scorer(tags: dict[str, Any]) -> int:
        aadt = resolve_aadt(tags, None)[0]
        return lts_from_tags(tags, aadt=aadt, operating_speed_kph=survey_speed_of(tags)).level

    disagreements = [
        (way_id, stored, scorer(tags))
        for way_id, stored, _confidence, _reasons, tags in scored
        if scorer(tags) != stored
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


def test_a_surveyed_way_reads_the_same_level_through_the_plan_time_store(
    conn: Any, polygon: str
) -> None:
    """ADR 0053 against the database: the `ways` layer a plan (or a freeze) reads carries
    the merged survey under the column the build scored with, so the level the scorer
    computes from it is the level in the graph. Skips until a region has a survey merged."""
    from longrun.core.data.postgis import PostGISLayerStore
    from longrun.core.routing.lts import SURVEY_SPEED_COLUMN, lts_for_way
    from longrun.core.scorers._common import row_tags

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (SURVEY_TABLE,))
        row = cur.fetchone()
        if not (row and row[0]):
            pytest.skip(f"no {SURVEY_TABLE}; no speed survey has been merged into any region")
        cur.execute(
            f"SELECT l.way_id, l.lts FROM {SCHEMA}.{TABLE} l JOIN {SURVEY_TABLE} s USING (way_id)"
        )
        stored = dict(cur.fetchall())
        if not stored:
            pytest.skip("the survey table is empty")
        # Wherever the survey was merged - the first was Oakland's (M21), not the Ozarks
        # this module's fixture polygon covers - so the box is drawn round surveyed ways.
        cur.execute(
            "SELECT ST_AsText(ST_Envelope(ST_Collect(geom))) FROM osm.ways "
            "WHERE way_id IN (SELECT way_id FROM " + SURVEY_TABLE + " ORDER BY way_id LIMIT 25)"
        )
        found = cur.fetchone()
    surveyed = found[0] if found and found[0] else polygon

    frame = PostGISLayerStore(conn)._intersecting("ways", surveyed)
    assert SURVEY_SPEED_COLUMN in frame.columns
    checked = 0
    for _, way in frame.iterrows():
        if int(way["way_id"]) in stored:
            assert lts_for_way(row_tags(way)).level == stored[int(way["way_id"])]
            checked += 1
    assert checked, "no surveyed way inside a box drawn round surveyed ways"
