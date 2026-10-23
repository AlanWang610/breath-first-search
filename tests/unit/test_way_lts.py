"""The offline LTS table's rules, against ways whose answer is known by construction.

The table itself needs PostGIS and lives in `tests/contract/test_way_lts_table.py`. What is
here is everything that decides *what* gets written, which is where a wrong answer would be
silent: a graph scored entirely LTS 2 and a graph scored correctly look identical from the
outside, and the only moment either is visible is the moment the row is built.
"""

from __future__ import annotations

from longrun.core.routing.lts import LTS_VERSION, lts_from_tags
from longrun.regions.lts import (
    SCHEMA,
    TABLE,
    LtsReport,
    composite_vintage,
    create_statements,
    resolve_aadt,
    score_way,
)


def test_a_way_with_no_highway_tag_gets_no_row() -> None:
    """The rule that keeps railways out of the graph at residential stress.

    `osm.ways` is filtered on `highway|railway|footway|cycleway`, so a mainline railway is in
    the table. `lts_from_tags` answers for any dict — an absent `highway` falls into its
    `unknown_highway_class` branch and returns a perfectly well-formed level 2 — so skipping
    has to be the caller's decision, and this is the test that it still is.
    """
    railway = {"railway": "rail", "name": "BNSF"}

    # The scorer does answer, which is the trap:
    assert lts_from_tags(railway).level == 2
    # ...and the table declines to record it.
    assert score_way(1, railway) is None
    assert score_way(2, {}) is None
    assert score_way(3, {"highway": ""}) is None


def test_a_road_gets_the_level_the_scorer_gives_it() -> None:
    """Agreement is the deliverable, so it is asserted rather than assumed."""
    for tags in (
        {"highway": "residential"},
        {"highway": "primary", "maxspeed": "55 mph"},
        {"highway": "footway"},
        {"highway": "secondary", "sidewalk": "both", "lanes": "4"},
        {"highway": "tertiary", "sidewalk": "separate"},
    ):
        row = score_way(10, tags)
        assert row is not None
        expected = lts_from_tags(tags, aadt=resolve_aadt(tags, None)[0])
        assert row.lts == expected.level
        assert row.confidence == expected.confidence
        assert row.reasons == expected.reasons


def test_the_reasons_are_stored_because_a_level_alone_is_unarguable() -> None:
    row = score_way(11, {"highway": "primary", "maxspeed": "55 mph"})
    assert row is not None
    assert row.lts == 4
    assert "highway:primary" in row.reasons
    # The two ways an LTS 4 can arise are distinguishable only through this list.
    assert any(reason.startswith("maxspeed_") for reason in row.reasons)


def test_a_separate_sidewalk_raises_stress_rather_than_lowering_it() -> None:
    """The disagreement that motivated the milestone, pinned so it cannot silently return.

    `add_lts_tags.py`'s placeholder read `sidewalk=separate` as a sidewalk and *decremented*;
    `has_sidewalk` returns False for it, because the sidewalk is mapped as its own way and
    this way is the roadway. On a fast arterial that is the difference between 2 and 4.
    """
    separate = score_way(12, {"highway": "secondary", "sidewalk": "separate"})
    both = score_way(13, {"highway": "secondary", "sidewalk": "both"})
    assert separate is not None and both is not None
    assert separate.lts > both.lts


class TestAadtPrecedence:
    """Conflated table, then OSM's own tag, then nothing — and which answered is recorded."""

    def test_nothing_is_the_normal_case(self) -> None:
        assert resolve_aadt({"highway": "primary"}, None) == (None, None)

    def test_the_osm_tag_is_used_when_no_table_has_conflated_one(self) -> None:
        # `hostility.py` already reads this tag through `aadt_of`, so a table that ignored it
        # would disagree with the scorer on exactly the ways that carry a volume.
        assert resolve_aadt({"highway": "primary", "aadt": "24000"}, None) == (24000.0, "osm_tag")

    def test_a_conflated_volume_wins(self) -> None:
        assert resolve_aadt({"highway": "primary", "aadt": "100"}, 24000.0) == (
            24000.0,
            "conflated",
        )

    def test_a_volume_moves_the_level_and_is_recorded_on_the_row(self) -> None:
        quiet = score_way(14, {"highway": "primary"}, conflated=800.0)
        busy = score_way(15, {"highway": "primary"}, conflated=30_000.0)
        assert quiet is not None and busy is not None
        assert quiet.lts < busy.lts
        assert quiet.aadt_source == "conflated"


class TestVintage:
    def test_the_vintage_names_both_the_extract_and_the_rules(self) -> None:
        assert composite_vintage("2026-09-13", version=1) == "2026-09-13+lts1"

    def test_it_defaults_to_the_current_version(self) -> None:
        assert composite_vintage("2026-09-13").endswith(f"+lts{LTS_VERSION}")

    def test_a_rules_change_changes_the_vintage_without_the_extract_moving(self) -> None:
        """The whole reason the version exists: the extract date pins the input only."""
        assert composite_vintage("2026-09-13", 1) != composite_vintage("2026-09-13", 2)


class TestSchema:
    def test_the_table_is_not_in_the_osm_schema(self) -> None:
        """`vintage()` sorts by `loaded_at` within a schema and ignores the source.

        A vintage row under `osm` would therefore become the answer every plan reports for
        `ways`, `nodes` and `amenities` — three layers made wrong by one row in the wrong
        place. Its own schema costs one `CREATE SCHEMA` and removes the failure mode.
        """
        assert SCHEMA == "osm_lts"
        assert not SCHEMA.startswith("osm.")

    def test_every_statement_is_idempotent(self) -> None:
        """A build resumes, so a step that cannot be re-run is a step that breaks resuming."""
        for statement in create_statements():
            assert "IF NOT EXISTS" in statement

    def test_there_is_no_geometry_column(self) -> None:
        """An attribute table on `osm.ways`. A second linestring is a second thing to drift."""
        ddl = " ".join(create_statements())
        assert f"{SCHEMA}.{TABLE}" in ddl
        assert "geometry(" not in ddl


class TestReport:
    def test_an_empty_report_does_not_divide_by_zero(self) -> None:
        assert "nothing new to score" in LtsReport(vintage="x+lts1").summary()

    def test_the_summary_separates_the_two_failure_modes(self) -> None:
        """ "Nothing was scored" and "everything was scored LTS 2" look identical in a graph."""
        report = LtsReport(vintage="2026-09-13+lts1", covered=10, scored=3, not_highway=7)
        report.levels.update([1, 2, 2])
        summary = report.summary()
        assert "10 way(s) at 2026-09-13+lts1" in summary
        assert "3 newly scored" in summary
        assert "7 without a highway tag skipped" in summary
        assert "lts2=2 (67%)" in summary

    def test_a_fully_covered_region_says_so_rather_than_reporting_zero(self) -> None:
        """The computation is incremental, so a rebuilt region scores nothing and is complete.

        Reporting only `scored` would print "0 way(s)" for a region that is entirely covered —
        indistinguishable in a build log from a region where the lookup returned nothing, which
        is the exact confusion this milestone exists to remove from the graph.
        """
        summary = LtsReport(vintage="2026-09-13+lts1", covered=1600, scored=0).summary()
        assert "1,600 way(s)" in summary
        assert "nothing new to score" in summary


# --- speed surveys (M18, ADR 0053) -------------------------------------------


def test_a_surveyed_way_gets_the_level_the_scorer_gives_it() -> None:
    """ADR 0025's forward hazard, closed for speed surveys: the build merges a survey into a
    way's tags under the column the plan-time `ways` layer carries it in, and both sides then
    read it through one function - so the row and the scorer cannot disagree."""
    from longrun.core.routing.lts import SURVEY_SPEED_COLUMN, lts_for_way
    from longrun.regions.lts import with_survey

    tags = {"highway": "secondary", "maxspeed": "40", "sidewalk": "both"}
    merged = with_survey(tags, 66.0, "portal.socrata.kcmo-speeds")
    row = score_way(12, merged)
    scorer = lts_for_way({**tags, SURVEY_SPEED_COLUMN: 66.0})
    assert row is not None
    assert (row.lts, row.confidence, row.reasons) == (
        scorer.level,
        scorer.confidence,
        scorer.reasons,
    )
    assert row.operating_speed_kph == 66.0
    assert row.speed_source == "portal.socrata.kcmo-speeds"


def test_a_way_without_a_survey_records_none() -> None:
    from longrun.regions.lts import with_survey

    tags = {"highway": "residential"}
    assert with_survey(tags, None, None) is tags
    row = score_way(13, tags)
    assert row is not None
    assert row.operating_speed_kph is None and row.speed_source is None


def test_the_graph_identity_names_a_survey_only_when_there_is_one() -> None:
    """A region with no survey keeps the identity it always had, so nothing cached is
    re-keyed; one with a survey is a different graph from the same extract."""
    assert composite_vintage("2026-09-14") == f"2026-09-14+lts{LTS_VERSION}"
    assert composite_vintage("2026-09-14", survey=None) == f"2026-09-14+lts{LTS_VERSION}"
    assert composite_vintage("2026-09-14", survey="a1b2c3d4") == (
        f"2026-09-14+lts{LTS_VERSION}+ssa1b2c3d4"
    )


def test_a_table_built_before_m18_gains_the_survey_columns_in_place() -> None:
    statements = " ".join(create_statements())
    assert "ADD COLUMN IF NOT EXISTS operating_speed_kph" in statements
    assert "ADD COLUMN IF NOT EXISTS speed_source" in statements


# --- a conflated volume (M18, ADR 0025's forward hazard) ---------------------


def test_a_conflated_volume_reaches_the_scorer_in_the_order_the_table_uses() -> None:
    """The table prefers `osm.way_aadt` to OSM's own tag. The plan-time `ways` layer now
    carries the conflation as `aadt_conflated`, read first by `aadt_of` - so the scorer and
    the table agree on a conflated way, which was the hazard ADR 0025 recorded."""
    from longrun.core.routing.lts import AADT_CONFLATED_COLUMN, aadt_of, lts_for_way

    tags = {"highway": "tertiary", "maxspeed": "50", "sidewalk": "both", "aadt": "1200"}
    row = score_way(14, tags, conflated=30_000.0)
    scorer = lts_for_way({**tags, AADT_CONFLATED_COLUMN: 30_000.0})
    assert row is not None
    assert (row.lts, row.reasons, row.aadt, row.aadt_source) == (
        scorer.level,
        scorer.reasons,
        30_000.0,
        "conflated",
    )
    assert aadt_of({**tags, AADT_CONFLATED_COLUMN: 30_000.0}) == 30_000.0
    assert resolve_aadt({**tags, AADT_CONFLATED_COLUMN: 30_000.0}, None) == (30_000.0, "conflated")
