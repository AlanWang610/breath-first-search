"""What the OSM loader keeps, and whether the scorers can ask for it (scope 13, ADR 0010).

`core.data.osm` splits into a vocabulary and an ingest, and the split is what makes this
file possible: everything here is a pure function of a tag dict, so it runs in CI on a bare
`uv sync` with no osmium, no 233 MB extract and no database.

The load is the expensive, once-per-region step, and its failure mode is quiet. A scorer
that asks `points_in_corridor(..., ["drinking_water"])` against a layer the loader never
put drinking water into gets an **empty frame**, which every scorer in this repo correctly
reads as "no water on this route" — a confident, wrong answer that no exception marks.
`test_every_kind_a_scorer_asks_for_is_a_kind_the_loader_keeps` is the guard, and it is the
reason this file exists rather than a handful of assertions inside the contract test.
"""

from __future__ import annotations

from longrun.core.data.osm import (
    AMENITY_KINDS,
    NODE_KINDS,
    TABLE_COLUMNS,
    TABLE_GEOMETRY,
    TABLE_KEY,
    TABLES,
    amenity_kind,
    create_statements,
    is_railway_line,
    node_kind,
    way_is_wanted,
)
from longrun.core.scorers.crossings import SIGNAL_KINDS
from longrun.core.scorers.services import SERVICE_CATEGORIES, all_kinds
from longrun.core.scorers.stop_density import STOP_KINDS


def _loadable_node_kinds() -> set[str]:
    return {value for values in NODE_KINDS.values() for value in values}


def _loadable_amenity_kinds() -> set[str]:
    return {value for values in AMENITY_KINDS.values() for value in values}


# --- the cross-check the milestone turns on ---------------------------------


def test_every_kind_a_scorer_asks_for_is_a_kind_the_loader_keeps() -> None:
    """The one test that stops a silent empty answer.

    `crossings` and `stop_density` filter `osm.nodes` by kind; `services` and
    `resupply_schedule` filter `osm.amenities`. A kind on one side of that and not the
    other is a scorer reporting "no signalized crossings" about a street full of them.

    Two vocabularies are deliberately *not* in the loader and are excluded here rather than
    silently passing: `services.SERVICE_CATEGORIES` includes the bare words `water`,
    `toilet` and `food`, which are category names it also matches against, not OSM tag
    values. No OSM object is tagged `amenity=food`.
    """
    categories = set(SERVICE_CATEGORIES)
    asked_of_nodes = set(SIGNAL_KINDS) | set(STOP_KINDS)
    asked_of_amenities = set(all_kinds()) - categories

    # `barrier` is a kind `stop_density` asks for as a catch-all; the loader stores the
    # specific barrier value instead, which is strictly more information.
    missing_nodes = asked_of_nodes - _loadable_node_kinds() - {"barrier"}
    missing_amenities = asked_of_amenities - _loadable_amenity_kinds()

    assert not missing_nodes, f"scorers ask osm.nodes for kinds the loader drops: {missing_nodes}"
    assert not missing_amenities, (
        f"scorers ask osm.amenities for kinds the loader drops: {missing_amenities}"
    )


def test_a_scorer_kind_list_and_the_loader_disagree_loudly_not_quietly() -> None:
    """Sabotage check on the test above: it has to be able to fail.

    A cross-check between two vocabularies is worth nothing if it passes for any pair, and
    this one is easy to write in a way that does — comparing supersets, say. This drops a
    kind and asserts the comparison notices.
    """
    reduced = _loadable_amenity_kinds() - {"drinking_water"}
    assert "drinking_water" in set(all_kinds()) - set(SERVICE_CATEGORIES)
    assert set(all_kinds()) - set(SERVICE_CATEGORIES) - reduced == {"drinking_water"}


# --- way selection ----------------------------------------------------------


def test_a_way_with_no_routing_tag_is_not_kept() -> None:
    assert not way_is_wanted({"building": "yes", "height": "12"})
    assert not way_is_wanted({"landuse": "grass"})


def test_every_key_a_scorer_reads_a_way_for_keeps_the_way() -> None:
    assert way_is_wanted({"highway": "residential"})
    assert way_is_wanted({"railway": "rail"})


def test_a_tram_in_a_street_is_not_a_railway_line() -> None:
    """The distinction `legality._check_railway` draws, applied at load time.

    Street trackage is a shared surface a runner crosses; a bare `railway=rail` linestring
    is a right of way. Both stay in `osm.ways` so `legality` can see them; only the second
    also becomes a `railways` row for `hazards` to test crossings against.
    """
    assert not is_railway_line({"railway": "tram", "highway": "secondary"})
    assert is_railway_line({"railway": "rail"})
    assert is_railway_line({"railway": "light_rail"})


def test_a_lifted_railway_is_not_a_railway_line() -> None:
    """An at-grade crossing of an abandoned line is not a hazard, and must not be flagged."""
    for value in ("abandoned", "disused", "razed", "construction", "proposed"):
        assert not is_railway_line({"railway": value}), value


# --- node and amenity classification ----------------------------------------


def test_a_control_node_gets_the_raw_osm_value_as_its_kind() -> None:
    """`crossings.is_signalized` compares against the raw value, so the loader stores it."""
    assert node_kind({"highway": "traffic_signals"}) == "traffic_signals"
    assert node_kind({"railway": "level_crossing"}) == "level_crossing"
    assert node_kind({"barrier": "lift_gate"}) == "lift_gate"


def test_an_ordinary_node_is_not_a_control_node() -> None:
    assert node_kind({}) is None
    assert node_kind({"highway": "residential"}) is None
    assert node_kind({"name": "somewhere"}) is None


def test_a_service_point_gets_the_raw_osm_value_as_its_kind() -> None:
    assert amenity_kind({"amenity": "drinking_water"}) == "drinking_water"
    assert amenity_kind({"shop": "supermarket"}) == "supermarket"
    assert amenity_kind({"man_made": "water_tap"}) == "water_tap"


def test_kind_resolution_follows_declaration_order() -> None:
    """A cafe with a shop counter is a cafe: `amenity` is declared before `shop`."""
    assert amenity_kind({"amenity": "cafe", "shop": "convenience"}) == "cafe"


def test_classification_tolerates_the_whitespace_and_case_real_osm_carries() -> None:
    assert amenity_kind({"amenity": " Drinking_Water "}) == "drinking_water"
    assert node_kind({"highway": "TRAFFIC_SIGNALS"}) == "traffic_signals"


def test_a_control_node_and_a_service_point_are_different_layers() -> None:
    """A node cannot be both, and the loader has to pick without asking the caller."""
    assert node_kind({"amenity": "toilets"}) is None
    assert amenity_kind({"highway": "traffic_signals"}) is None


# --- schema -----------------------------------------------------------------


def test_the_ddl_is_idempotent() -> None:
    """A region build re-runs its steps; DDL that is not idempotent is not resumable."""
    statements = create_statements()
    creates = [s for s in statements if s.startswith("CREATE")]
    assert creates, "no DDL emitted"
    assert all("IF NOT EXISTS" in s for s in creates), [s for s in creates if "IF NOT" not in s]


def test_every_table_is_indexed_on_geometry() -> None:
    """The one index the scope 6.4 budget rests on: every scorer starts with a corridor."""
    statements = " ".join(create_statements("scratch"))
    for table in TABLES:
        assert f"ON scratch.{table} USING GIST (geom)" in statements, table


def test_the_kind_filtered_layers_are_indexed_on_kind() -> None:
    statements = " ".join(create_statements())
    for table in ("nodes", "amenities"):
        assert f"ON osm.{table} (kind)" in statements, table


def test_the_schema_is_described_once_and_consistently() -> None:
    """Four parallel dicts key the schema; a table missing from one is a runtime error."""
    assert set(TABLE_COLUMNS) == set(TABLE_GEOMETRY) == set(TABLE_KEY) == set(TABLES)
    for table, key in TABLE_KEY.items():
        assert f"{key} bigint PRIMARY KEY" in TABLE_COLUMNS[table], table


def test_the_layer_names_match_what_the_store_maps() -> None:
    """`DEFAULT_LAYER_TABLES` is the contract; a loader writing elsewhere loads nothing."""
    from longrun.core.data.postgis import DEFAULT_LAYER_TABLES

    for table in TABLES:
        assert DEFAULT_LAYER_TABLES.get(table) == f"osm.{table}", table
