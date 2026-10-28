"""Tier 3 as data: catalog rows, their five mapping kinds, and records into features (M21)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from longrun.adapters.base import LoadFailure
from longrun.adapters.portals.catalog import (
    CATALOG,
    CatalogAdapter,
    CatalogEntry,
    load,
    read,
    to_features,
)
from longrun.adapters.registry import expand


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": "portal.socrata.test-closures",
        "jurisdiction": "tiger:place:2938000",
        "kind": "closures",
        "platform": "socrata",
        "domain": "data.example.gov",
        "dataset": "abcd-1234",
        "source": "portal_test",
        "zone": "America/Chicago",
        "geometry": {"kind": "field", "field": "the_geom"},
        "category": {"kind": "const", "value": "road-closure"},
        "start": {"kind": "field", "field": "start_date", "format": "date"},
        "end": {"kind": "field", "field": "end_date", "format": "date"},
        "detail": {"kind": "template", "template": "{street} from {cross_a} to {cross_b}"},
        "ref": {"kind": "field", "field": "permit"},
        "licence": {"id": None, "read_on": "2026-10-28", "from": "https://data.example.gov/x"},
        "survey": {
            "on": "2026-10-28",
            "rows": 10,
            "updated": "2026-10-27",
            "max_staleness_days": 14,
        },
    }
    row.update(overrides)
    return row


def _write(tmp_path: Path, adopted: list[Any], refused: list[Any] | None = None) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"adopted": adopted, "refused": refused or []}), "utf-8")
    return path


def _entry(**overrides: Any) -> CatalogEntry:
    return CatalogEntry.model_validate(_row(**overrides))


# --- rows into adapters -----------------------------------------------------


def test_a_row_becomes_a_tier_three_adapter_the_registry_accepts(tmp_path: Path) -> None:
    loaded = load(_write(tmp_path, [_row()]))
    adapters, failures = expand("portal-catalog", loaded)
    assert failures == []
    (adapter,) = adapters
    assert isinstance(adapter, CatalogAdapter)
    assert (adapter.name, adapter.kind, adapter.tier) == (
        "portal.socrata.test-closures",
        "closures",
        3,
    )
    assert adapter.jurisdictions == ("tiger:place:2938000",)
    assert (adapter.scope, adapter.complete, adapter.source) == ("polygon", True, "portal_test")
    assert not hasattr(adapter, "key"), "a token raises a rate limit; it never skips a rung"


@pytest.mark.parametrize(
    ("override", "complaint"),
    [
        ({"name": "socrata.test"}, "portal.<platform>.<slug>"),
        ({"name": "portal.arcgis.test"}, "does not name its platform"),
        ({"jurisdiction": "tiger:city:1"}, "malformed jurisdiction"),
        ({"source": "test"}, "portal_<publisher>"),
        ({"dataset": "not-an-id"}, "not a Socrata dataset id"),
        ({"zone": "Mars/Olympus"}, "unknown zone"),
        ({"geometry": {"kind": "const", "value": "x"}}, "geometry takes field/point"),
        ({"start": {"kind": "field", "field": "start_date"}}, "must state its timestamp format"),
        ({"category": {"kind": "regex", "field": "x", "pattern": "."}}, "kind"),
        ({"measures": {"speed_85_kph": {"kind": "field", "field": "s", "unit": "mph"}}}, "only a"),
        ({"kind": "access_hours"}, "maps hours"),
        ({"confidence": 0.4}, "greater than 0.5"),
    ],
)
def test_a_bad_row_is_one_named_failure_and_the_rest_still_load(
    tmp_path: Path, override: dict[str, Any], complaint: str
) -> None:
    """A sixth mapping kind is refused like any other malformed row: the cap on the mapping
    language is the design (ADR 0060), and a row that needs more is a Python adapter."""
    good = _row(name="portal.socrata.good")
    loaded = load(_write(tmp_path, [good, _row(**override)]))
    adapters = [a for a in loaded if isinstance(a, CatalogAdapter)]
    failures = [f for f in loaded if isinstance(f, LoadFailure)]
    assert [a.name for a in adapters] == ["portal.socrata.good"]
    assert len(failures) == 1
    assert failures[0].name.startswith("catalog[")
    assert complaint in failures[0].reason


def test_a_speed_survey_states_its_measure_and_unit() -> None:
    row = _row(kind="speed_survey", category={"kind": "const", "value": "speed-survey"})
    with pytest.raises(ValueError, match="maps measures"):
        CatalogEntry.model_validate(row)
    unitless = {"speed_85_kph": {"kind": "field", "field": "s85"}}
    with pytest.raises(ValueError, match="states its unit"):
        CatalogEntry.model_validate({**row, "measures": unitless})
    measured = {"speed_85_kph": {"kind": "field", "field": "s85", "unit": "mph"}}
    entry = CatalogEntry.model_validate({**row, "measures": measured})
    assert entry.effective_confidence() == 0.85, "ADR 0053 merges nothing under 0.8"
    assert _entry().effective_confidence() == 0.7


def test_two_rows_with_one_name_are_both_refused(tmp_path: Path) -> None:
    loaded = load(_write(tmp_path, [_row(), _row()]))
    assert [type(x) for x in loaded] == [LoadFailure]
    assert loaded[0].reason == "adopted twice"


def test_an_unreadable_catalog_is_a_failure_not_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text("adopted: [unclosed", "utf-8")
    loaded = load(path)
    assert len(loaded) == 1 and isinstance(loaded[0], LoadFailure)
    assert load(tmp_path / "missing.yaml")[0].reason.startswith("unreadable")


def test_a_refusal_states_a_closed_reason_and_what_was_checked(tmp_path: Path) -> None:
    good = {
        "jurisdiction": "tiger:place:3651000",
        "kind": "speed_survey",
        "reason": "points_only",
        "checked": ["data.cityofnewyork.us: 'speed'"],
        "on": "2026-10-28",
        "note": "counts at stations",
    }
    vague = {**good, "reason": "didn't get to it"}
    unchecked = {**good, "kind": "closures", "checked": []}
    catalog = read(_write(tmp_path, [], [good, vague, unchecked]))
    assert [r.reason for r in catalog.refused] == ["points_only"]
    assert len(catalog.failures) == 2
    assert all(f.name.startswith("catalog.refused[") for f in catalog.failures)
    assert load(_write(tmp_path, [], [vague])) == [], "a refusal never becomes an adapter"


def test_the_packaged_catalog_reads_whole() -> None:
    catalog = read(CATALOG)
    assert catalog.failures == [], [f"{f.name}: {f.reason}" for f in catalog.failures]


# --- records into features --------------------------------------------------


def test_a_record_maps_through_field_const_and_template() -> None:
    record = {
        "the_geom": {"type": "MultiLineString", "coordinates": [[[-94.58, 39.1], [-94.57, 39.1]]]},
        "start_date": "2026-10-27T00:00:00.000",
        "end_date": "2026-10-28T00:00:00.000",
        "street": "MAIN ST",
        "cross_a": "1ST ST",
        "cross_b": None,
        "permit": "P-1",
    }
    mapped = to_features(_entry(), [record])
    assert mapped.dropped == 0
    (feature,) = mapped.features
    assert (feature.kind, feature.category, feature.tier, feature.confidence) == (
        "closures",
        "road-closure",
        3,
        0.7,
    )
    assert feature.geometry["type"] == "MultiLineString"
    assert feature.detail == "MAIN ST from 1ST ST to", "an empty column leaves no gap"
    assert feature.ref == "P-1"
    assert feature.jurisdiction == "tiger:place:2938000"
    assert feature.source_url == "https://data.example.gov/d/abcd-1234"
    # A date is the whole local day (CDT is UTC-5): from local midnight on the first to the
    # last second of the second, as naive UTC (ADR 0046).
    assert feature.start == datetime(2026, 10, 27, 5, 0)
    assert feature.end == datetime(2026, 10, 29, 4, 59, 59)


def test_an_instant_with_an_offset_names_the_local_day_it_falls_on() -> None:
    """Dallas stores a date as local midnight in UTC: 05:00Z is the start of the day in
    Chicago, not five hours into it."""
    record = {
        "the_geom": {"type": "Point", "coordinates": [-96.8, 32.8]},
        "start_date": "2026-03-15T05:00:00.000Z",
        "end_date": "2026-03-15T05:00:00.000Z",
    }
    (feature,) = to_features(_entry(), [record]).features
    assert feature.start == datetime(2026, 3, 15, 5, 0)
    assert feature.end == datetime(2026, 3, 16, 4, 59, 59)


@pytest.mark.parametrize(
    ("fmt", "value", "expected"),
    [
        ("local", "2026-07-01T09:30:00", datetime(2026, 7, 1, 14, 30)),
        ("utc", "2026-07-01T09:30:00Z", datetime(2026, 7, 1, 9, 30)),
        ("utc", "2026-07-01T09:30:00", datetime(2026, 7, 1, 9, 30)),
        ("epoch_ms", 1782898200000, datetime(2026, 7, 1, 9, 30)),
        ("epoch_ms_date", 1782864000000, datetime(2026, 7, 1, 5, 0)),
    ],
)
def test_each_timestamp_format_reads_as_naive_utc(fmt: str, value: Any, expected: datetime) -> None:
    entry = _entry(start={"kind": "field", "field": "s", "format": fmt}, end=None)
    record = {"the_geom": {"type": "Point", "coordinates": [-94.5, 39.1]}, "s": value}
    (feature,) = to_features(entry, [record]).features
    assert feature.start == expected


def test_a_legacy_location_and_a_point_mapping_both_become_points() -> None:
    legacy = {"loc": {"latitude": "41.88", "longitude": "-87.63", "human_address": "{}"}}
    entry = _entry(geometry={"kind": "field", "field": "loc"}, start=None, end=None)
    (feature,) = to_features(entry, [legacy]).features
    assert feature.geometry == {"type": "Point", "coordinates": [-87.63, 41.88]}

    numeric = _entry(geometry={"kind": "point", "lat": "lat", "lon": "lon"}, start=None, end=None)
    (feature,) = to_features(numeric, [{"lat": "41.88", "lon": -87.63}]).features
    assert feature.geometry["coordinates"] == [-87.63, 41.88]
    assert to_features(numeric, [{"lat": 0, "lon": 0}]).dropped == 1, "null island is no place"


def test_a_category_nobody_mapped_drops_the_record_unless_there_is_a_default() -> None:
    mapping = {"kind": "map", "field": "closure", "values": {"Full": "road-closure"}}
    entry = _entry(category=mapping, start=None, end=None)
    point = {"type": "Point", "coordinates": [-87.6, 41.9]}
    mapped = to_features(
        entry, [{"the_geom": point, "closure": "Full"}, {"the_geom": point, "closure": "Curb"}]
    )
    assert [f.category for f in mapped.features] == ["road-closure"]
    assert mapped.dropped == 1 and "not in the category map" in (mapped.first_problem or "")

    lenient = _entry(category={**mapping, "default": "some-lanes-closed"}, start=None, end=None)
    assert [
        f.category for f in to_features(lenient, [{"the_geom": point, "closure": "Curb"}]).features
    ] == ["some-lanes-closed"]


def test_a_survey_in_mph_becomes_kph_and_a_point_survey_is_dropped() -> None:
    entry = _entry(
        kind="speed_survey",
        category={"kind": "const", "value": "speed-survey"},
        start=None,
        end=None,
        detail=None,
        label={"kind": "field", "field": "street"},
        observed_on={"kind": "field", "field": "counted", "format": "date"},
        measures={"speed_85_kph": {"kind": "field", "field": "p85", "unit": "mph"}},
    )
    line = {"type": "LineString", "coordinates": [[-94.58, 39.1], [-94.57, 39.1]]}
    record = {"the_geom": line, "p85": "35", "street": "Main St", "counted": "2025-05-07T00:00:00"}
    (feature,) = to_features(entry, [record]).features
    assert feature.measures == {"speed_85_kph": 56.33}
    assert (feature.name, feature.observed_on) == ("Main St", date(2025, 5, 7))
    assert feature.confidence == 0.85

    station = {**record, "the_geom": {"type": "Point", "coordinates": [-94.58, 39.1]}}
    mapped = to_features(entry, [station])
    assert mapped.dropped == 1 and "line geometry" in (mapped.first_problem or "")


def test_a_record_without_geometry_is_dropped_and_counted() -> None:
    mapped = to_features(_entry(), [{"start_date": "2026-10-27T00:00:00"}, {"the_geom": None}])
    assert (mapped.features, mapped.dropped, mapped.first_problem) == ([], 2, "no geometry")
