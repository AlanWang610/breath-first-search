"""The national sources, minus the network (scope 13 step 2).

`core.data.national` splits the way `core.data.osm` does: the parts that decide *what* gets
loaded and *where it goes* are pure and run in CI, and the parts that fetch 121 MB and talk
to PostGIS are `network`-marked.

The two tests that earn their place are the contract ones. A loader that writes
`tiger.boundary` when the store reads `tiger.boundaries` produces a database full of data
and a scorer that reports `unavailable`, with nothing anywhere raising — the same silent
shape as a kind list that does not match, which `test_osm_vocabulary` guards for OSM.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiLineString, Polygon

from longrun.core.data.national import (
    NHD,
    NHD_CROSSABLE_FTYPES,
    SOURCES,
    TIGER,
    TIGER_LEVELS,
    TIGER_YEAR,
    _as_multi,
    create_statements,
    normalise_nhd,
    normalise_tiger,
)
from longrun.core.data.postgis import DEFAULT_LAYER_TABLES
from longrun.core.export import attribution

# --- the contracts ----------------------------------------------------------


def test_every_source_writes_the_table_the_store_reads() -> None:
    """The loader's target and the store's source have to be the same string.

    They were written months apart — `DEFAULT_LAYER_TABLES` at M1.4, these loaders at M3 —
    and a mismatch is invisible: the load succeeds, the table fills, and every scorer that
    wants the layer reports `unavailable` because `to_regclass` says the *other* name does
    not exist.
    """
    for spec in SOURCES:
        assert DEFAULT_LAYER_TABLES.get(spec.layer) == spec.qualified, spec.layer


def test_every_source_has_a_licence() -> None:
    """Scope 14 is an obligation, and an unattributed source prints on the sheet."""
    for spec in SOURCES:
        licence = attribution.licence_for(spec.source)
        assert licence is not None, spec.source
        assert licence.licence == spec.licence, (spec.source, licence.licence, spec.licence)


def test_the_layer_names_resolve_to_the_same_licence_as_the_source() -> None:
    """A scorer records the layer it read, not the source, so both have to resolve."""
    for spec in SOURCES:
        by_layer = attribution.licence_for(spec.layer)
        assert by_layer is not None and by_layer.source == spec.source, spec.layer


# --- URLs -------------------------------------------------------------------


def test_the_tiger_year_is_pinned_not_computed() -> None:
    """Scope 6.4 wants two plans comparable. A year derived from today's date changes
    under a plan the day the Census publishes, for no reason the plan records."""
    assert TIGER_YEAR.isdigit() and len(TIGER_YEAR) == 4
    assert TIGER.vintage == f"tiger-{TIGER_YEAR}"


def test_the_tiger_url_is_built_for_each_level() -> None:
    kind, scope = TIGER_LEVELS["county"]
    url = TIGER.url(year=TIGER_YEAR, kind=kind, scope=scope, lower="county")
    assert url.endswith(f"/COUNTY/tl_{TIGER_YEAR}_us_county.zip")


def test_places_are_published_per_state_and_the_table_says_so() -> None:
    """The reason `load_tiger` demands a state list rather than defaulting to the nation."""
    assert TIGER_LEVELS["place"][1] is None
    assert TIGER_LEVELS["state"][1] == "us"


def test_the_nhd_url_is_built_per_watershed() -> None:
    assert NHD.url(huc4="1805").endswith("/NHD_H_1805_HU4_GPKG.zip")


# --- schema -----------------------------------------------------------------


def test_the_ddl_is_idempotent_and_indexed() -> None:
    for spec in SOURCES:
        statements = create_statements(spec)
        assert all("IF NOT EXISTS" in s for s in statements), spec.layer
        joined = " ".join(statements)
        assert f"ON {spec.qualified} USING GIST (geom)" in joined, spec.layer
        assert f"{spec.key} text PRIMARY KEY" in joined, spec.layer


def test_the_declared_columns_reach_the_ddl() -> None:
    joined = " ".join(create_statements(NHD))
    for name, sql_type in NHD.columns.items():
        assert f"{name} {sql_type}" in joined, name


# --- geometry promotion -----------------------------------------------------


def test_a_single_geometry_is_promoted_to_its_multi_form() -> None:
    """NHD publishes LineString and MultiLineString in one table, and a typed PostGIS
    column takes one of them. Promoting keeps the type check that catches a polygon
    written into a line layer."""
    line = LineString([(0, 0), (1, 1)])
    promoted = _as_multi(line, "MultiLineString")
    assert promoted is not None and promoted.geom_type == "MultiLineString"
    assert _as_multi(Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]), "MultiPolygon") is not None


def test_a_three_dimensional_geometry_is_flattened() -> None:
    """NHD publishes measured 3D linestrings, and the column is 2D.

    Found by the load failing with "Geometry has Z dimension but column does not" on all
    110,982 Bay Area flowlines. Flattening is deliberate: the z is the stream bed's
    elevation and `hazards` asks a plan-view question - does the route line cross the
    watercourse - so carrying it doubles the column for something nothing reads.
    """
    line = LineString([(0, 0, 5), (1, 1, 7)])
    assert line.has_z
    flattened = _as_multi(line, "MultiLineString")
    assert flattened is not None and not flattened.has_z


def test_an_already_multi_geometry_is_flattened_too() -> None:
    """The early return for a matching type is where a Z would slip through."""
    multi = MultiLineString([[(0, 0, 5), (1, 1, 7)]])
    out = _as_multi(multi, "MultiLineString")
    assert out is not None and not out.has_z


def test_an_already_multi_geometry_keeps_its_coordinates() -> None:
    """Equality, not identity: `force_2d` returns a new object even for a 2D input, and
    asserting identity would fail for a coercion that changed nothing."""
    multi = MultiLineString([[(0, 0), (1, 1)]])
    assert _as_multi(multi, "MultiLineString").equals(multi)


def test_a_geometry_of_the_wrong_family_is_dropped_not_coerced() -> None:
    """A polygon in a flowline frame is a source error, and inventing a line from it would
    put a fictional watercourse across a route."""
    assert _as_multi(Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]), "MultiLineString") is None
    assert _as_multi(None, "MultiLineString") is None


# --- normalisers ------------------------------------------------------------


def _frame(records: list[dict], geometry: list) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(records, geometry=geometry, crs="EPSG:4326")


def test_tiger_columns_are_lowercased_and_the_level_is_stamped() -> None:
    frame = _frame(
        [{"GEOID": "06075", "NAME": "San Francisco", "STATEFP": "06", "LSAD": "06"}],
        [Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])],
    )
    out = normalise_tiger(frame, "county")
    assert out.iloc[0]["geoid"] == "06075"
    assert out.iloc[0]["name"] == "San Francisco"
    assert out.iloc[0]["level"] == "county"


def test_tiger_falls_back_to_namelsad_when_there_is_no_plain_name() -> None:
    frame = _frame(
        [{"GEOID": "06075", "NAMELSAD": "San Francisco County"}],
        [Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])],
    )
    out = normalise_tiger(frame, "county")
    assert out.iloc[0]["name"] == "San Francisco County"


def test_tiger_supplies_the_declared_columns_even_when_the_source_omits_them() -> None:
    """A source that stops publishing a column must not become a table of NULLs.

    `_rows` reads each declared column with `Series.get`, which answers None for a label
    that is not there - so the load succeeds and the first sign of the loss is a plan
    sheet naming an unnamed jurisdiction. Filling the columns in the normaliser puts the
    absence in one place, before anything is written.
    """
    frame = _frame([{"GEOID": "06"}], [Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])])
    out = normalise_tiger(frame, "state")
    for column in TIGER.columns:
        assert column in out.columns, column


def test_nhd_keeps_only_the_water_a_runner_could_have_to_cross() -> None:
    """A pipeline is not a ford and a coastline is not a crossing. Keeping them would put
    a water-crossing hazard on every waterfront path in the country."""
    frame = _frame(
        [
            {"permanent_identifier": "a", "ftype": 460, "fcode": 46006},  # stream/river
            {"permanent_identifier": "b", "ftype": 428, "fcode": 42800},  # pipeline
            {"permanent_identifier": "c", "ftype": 566, "fcode": 56600},  # coastline
        ],
        [LineString([(0, 0), (1, 1)])] * 3,
    )
    out = normalise_nhd(frame, "1805")
    assert list(out["permanent_identifier"]) == ["a"]
    assert out.iloc[0]["huc4"] == "1805"


def test_the_kept_feature_types_include_the_ordinary_stream() -> None:
    """460 is StreamRiver. If this ever falls out of the set, every water crossing in the
    country stops being reported and nothing else changes."""
    assert 460 in NHD_CROSSABLE_FTYPES


@pytest.mark.parametrize("excluded", [428, 420, 566])
def test_the_excluded_feature_types_stay_excluded(excluded: int) -> None:
    assert excluded not in NHD_CROSSABLE_FTYPES


# --- PAD-US -----------------------------------------------------------------


def test_padus_writes_the_table_the_store_reads() -> None:
    from longrun.core.data.padus import PADUS

    assert DEFAULT_LAYER_TABLES["parks"] == PADUS.qualified


def test_the_padus_query_names_the_input_projection() -> None:
    """The quiet failure: without `inSR` the server reads a WGS84 envelope in the *layer's*
    projection, which for California selects a box in the Pacific and returns zero features
    rather than an error."""
    from longrun.core.data.padus import query_params
    from longrun.core.models.geometry import BBox

    params = query_params(BBox(min_lon=-122.5, min_lat=37.7, max_lon=-122.3, max_lat=37.9))
    assert params["inSR"] == "4326"
    assert params["outSR"] == "4326"
    assert params["geometry"] == "-122.5,37.7,-122.3,37.9"
    assert params["geometryType"] == "esriGeometryEnvelope"


def test_the_padus_query_is_sorted_so_paging_is_defined() -> None:
    """ArcGIS documents `resultOffset` as requiring a sort; without one a second page may
    repeat or skip rows from the first."""
    from longrun.core.data.padus import query_params
    from longrun.core.models.geometry import BBox

    params = query_params(BBox(min_lon=-1.0, min_lat=1.0, max_lon=1.0, max_lat=2.0), offset=1000)
    assert params["orderByFields"]
    assert params["resultOffset"] == 1000


def test_the_agency_columns_are_requested() -> None:
    """`Mang_Name` is what resolves a park to an adapter (scope 13 step 4). Without it the
    layer answers containment and nothing else."""
    from longrun.core.data.padus import query_params
    from longrun.core.models.geometry import BBox

    fields = query_params(BBox(min_lon=-1.0, min_lat=1.0, max_lon=1.0, max_lat=2.0))["outFields"]
    assert "Mang_Name" in fields
    assert "Unit_Nm" in fields


def test_an_aggregate_row_is_split_into_its_parts() -> None:
    """The defect the first real corridor produced: two unnamed `CITY` rows of 15,243 and
    18,573 parts were 19.7 MB of a 19.9 MB fixture holding 106 features.

    Kept whole, such a row answers a corridor query with "somewhere inside this blob there
    is a park near you" and costs a point-in-polygon test against all of it.
    """
    from shapely.geometry import MultiPolygon, Polygon

    from longrun.core.data.padus import _parts

    a = Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])
    b = Polygon([(5, 5), (6, 5), (6, 6), (5, 5)])
    assert len(_parts(MultiPolygon([a, b]))) == 2
    assert len(_parts(a)) == 1


def test_an_empty_part_is_dropped_rather_than_written() -> None:
    from shapely.geometry import Polygon

    from longrun.core.data.padus import _parts

    assert _parts(Polygon()) == []


def test_every_part_of_an_aggregate_keeps_its_agency_and_gets_its_own_id() -> None:
    """An aggregate row is one *manager*, not one place - so the agency is copied to every
    part, and the id has to distinguish them or the upsert keeps only the last."""
    from longrun.core.data.padus import _normalise

    properties = {"Unit_Nm": "", "Mang_Name": "CITY", "Mang_Type": "LOC"}
    first = _normalise(properties, 3, 0)
    second = _normalise(properties, 3, 1)
    assert first["agency"] == second["agency"] == "CITY"
    assert first["unit_id"] != second["unit_id"]


def test_a_row_with_no_name_is_still_loaded() -> None:
    """PAD-US publishes protected land with no name and no manager recorded. It is still
    protected land, and `services_along` asks only whether a point is inside it."""
    from longrun.core.data.padus import _normalise

    row = _normalise({}, 0, 0)
    assert row["name"] == "unnamed"
    assert row["agency"] == "unknown"
    assert row["unit_id"]
