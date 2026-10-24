"""`speed_survey.way_speed` against the live database (M18, ADR 0053).

The matcher's rules are unit-tested on hand-built lines. What only a database can get wrong
is the round trip: candidate ways found by the index box, the row written, the region's rows
replaced whole, and the vintage recorded where `regions/lts` reads it. One real Ozarks way
is surveyed along its own geometry under a throwaway region name, and everything written
under that name is removed afterwards.

    docker compose -f deploy/docker-compose.yml up -d
    uv run longrun build-region deploy/regions/ozarks.yaml
    uv run pytest tests/contract/test_speed_survey_table.py -m network
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.network

DSN = os.environ.get("LONGRUN_POSTGIS_DSN", "postgresql://longrun:longrun@localhost:5432/longrun")
REGION = "contract-test-m18"


@pytest.fixture
def conn() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no PostGIS at {DSN}: {exc}")
    with connection.cursor() as cur:
        cur.execute("SELECT to_regnamespace('speed_survey')")
        row = cur.fetchone()
    existed = bool(row and row[0])
    try:
        yield connection
    finally:
        with connection.cursor() as cur:
            cur.execute("DELETE FROM meta.layer_vintage WHERE region = %s", (REGION,))
            if existed:
                cur.execute("DELETE FROM speed_survey.way_speed WHERE region = %s", (REGION,))
            else:
                # Left as found: a database no build has merged a survey into has no schema,
                # and an empty one would put two null columns on every frozen `ways` layer.
                cur.execute("DROP SCHEMA IF EXISTS speed_survey CASCADE")
        connection.commit()
        connection.close()


def test_a_survey_along_a_real_way_is_merged_onto_it(conn: Any) -> None:
    from shapely import from_wkb
    from shapely.geometry import mapping

    from longrun.core.models.features import Feature
    from longrun.regions import speed_surveys
    from longrun.regions.lts import survey_vintage

    with conn.cursor() as cur:
        cur.execute(
            "SELECT way_id, tags->>'name', ST_AsBinary(geom) FROM osm.ways "
            "WHERE tags->>'highway' IN ('secondary', 'tertiary') AND ST_Length(geom::geography) "
            "BETWEEN 200 AND 2000 ORDER BY way_id LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no secondary or tertiary way loaded; run build-region first")
    way_id, name, wkb = row
    survey = Feature(
        kind="speed_survey",
        category="speed_survey",
        geometry=mapping(from_wkb(bytes(wkb))),
        tier=3,
        confidence=0.9,
        measures={"speed_85_kph": 63.0},
        name=name,
        source_url="https://example.invalid/contract-test",
    )

    report = speed_surveys.compute(conn, region=REGION, surveys=[survey])
    assert report.merged >= 1 and report.changed

    with conn.cursor() as cur:
        cur.execute(
            "SELECT speed_85_kph, vintage FROM speed_survey.way_speed "
            "WHERE way_id = %s AND region = %s",
            (way_id, REGION),
        )
        stored = cur.fetchone()
    assert stored is not None and stored[0] == pytest.approx(63.0)
    assert survey_vintage(conn, REGION) == stored[1] == report.vintage

    # The same surveys again change nothing, and an empty answer clears the region.
    assert not speed_surveys.compute(conn, region=REGION, surveys=[survey]).changed
    cleared = speed_surveys.compute(conn, region=REGION, surveys=[])
    assert cleared.changed and cleared.merged == 0
    assert survey_vintage(conn, REGION) is None
