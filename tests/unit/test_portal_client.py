"""Asking a portal: the cache key, Socrata's and ArcGIS's queries, paging and staleness (M21)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from shapely.geometry import box

from longrun.adapters.base import AdapterContext
from longrun.adapters.portals import arcgis, client, socrata
from longrun.adapters.portals.catalog import CatalogEntry
from longrun.adapters.portals.client import fetch_portal, portal_args, snap_bbox, week_of
from longrun.core.data.cache import SqliteCache, args_hash
from longrun.core.models.context import Budget

DAY = date(2026, 10, 28)
CORRIDOR = box(-94.585, 39.093, -94.571, 39.104)


def _entry(**overrides: Any) -> CatalogEntry:
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
        "licence": {"id": None, "read_on": "2026-10-28", "from": "https://data.example.gov/x"},
        "survey": {
            "read_on": "2026-10-28",
            "rows": 1,
            "updated": "2026-10-27",
            "max_staleness_days": 14,
        },
    }
    row.update(overrides)
    return CatalogEntry.model_validate(row)


def _arcgis(**overrides: Any) -> CatalogEntry:
    return _entry(
        name="portal.arcgis.test-closures",
        platform="arcgis",
        domain="services.example.com",
        dataset="/abc/arcgis/rest/services/Closures/FeatureServer/0",
        geometry={"kind": "field", "field": "geometry"},
        start={"kind": "field", "field": "StartDate", "format": "epoch_ms"},
        end={"kind": "field", "field": "EndDate", "format": "epoch_ms"},
        **overrides,
    )


def _ctx(cache: Any, budget: Budget | None = None) -> AdapterContext:
    return AdapterContext(cache=cache, budget=budget or Budget(), offline=False)


def _epoch(day: date) -> float:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp()


LINE = {"type": "LineString", "coordinates": [[-94.58, 39.1], [-94.575, 39.1]]}
ROW = {
    "the_geom": LINE,
    "start_date": "2026-10-27T00:00:00.000",
    "end_date": "2026-11-02T00:00:00.000",
}


# --- the cache key ----------------------------------------------------------


def test_a_box_is_snapped_outward_so_nearby_routes_share_a_key() -> None:
    assert snap_bbox(CORRIDOR.bounds) == (-94.59, 39.09, -94.57, 39.11)
    nudged = box(-94.584, 39.094, -94.572, 39.103)
    assert snap_bbox(nudged.bounds) == snap_bbox(CORRIDOR.bounds)
    assert snap_bbox(None) is None
    assert snap_bbox((1.0, 2.0, float("nan"), 3.0)) is None


def test_the_cache_key_is_pinned() -> None:
    """A recorded cassette is found by this hash. Changing `portal_args` orphans every one,
    so it changes deliberately, with this number, or not at all."""
    args = portal_args("portal.socrata.test-closures", (-94.59, 39.09, -94.57, 39.11), DAY)
    assert args == {
        "adapter": "portal.socrata.test-closures",
        "bbox": [-94.59, 39.09, -94.57, 39.11],
        "day": "2026-10-28",
    }
    assert args_hash(args) == args_hash(json.loads(json.dumps(args)))
    assert args_hash(args)[:12] == "76fca509f099"


def test_the_metadata_is_read_once_a_week() -> None:
    assert week_of(date(2026, 10, 28)) == date(2026, 10, 26)
    assert week_of(date(2026, 11, 1)) == date(2026, 10, 26)


# --- Socrata ----------------------------------------------------------------


def test_socrata_asks_for_the_corridor_and_the_window() -> None:
    clause = socrata.where(
        _entry(where="permit_type = 'Closure'"), (-94.59, 39.09, -94.57, 39.11), DAY
    )
    assert clause == (
        "(permit_type = 'Closure') AND "
        "intersects(the_geom, 'POLYGON((-94.59 39.09, -94.57 39.09, -94.57 39.11, -94.59 39.11, "
        "-94.59 39.09))') AND "
        "(end_date IS NULL OR end_date >= '2026-10-27T00:00:00') AND "
        "(start_date IS NULL OR start_date <= '2026-10-29T23:59:59')"
    )


def test_socrata_asks_a_legacy_location_and_a_point_mapping_the_way_each_can_answer() -> None:
    bbox = (-87.64, 41.87, -87.62, 41.89)
    legacy = _entry(geometry={"kind": "field", "field": "location"}, spatial="within_box")
    assert socrata.where(legacy, bbox, DAY).startswith(
        "within_box(location, 41.89, -87.64, 41.87, -87.62)"
    )
    numeric = _entry(geometry={"kind": "point", "lat": "latitude", "lon": "longitude"})
    assert socrata.where(numeric, bbox, DAY).startswith(
        "latitude BETWEEN 41.87 AND 41.89 AND longitude BETWEEN -87.64 AND -87.62"
    )
    textual = _entry(window_filter=False)
    assert "end_date" not in (socrata.where(textual, bbox, DAY) or "")
    assert socrata.where(_entry(window_filter=False), None, DAY) is None


def _socrata_routes(rows: list[Any], updated: date = date(2026, 10, 27)) -> respx.Route:
    respx.get("https://data.example.gov/api/views/abcd-1234.json").mock(
        return_value=httpx.Response(200, json={"rowsUpdatedAt": _epoch(updated), "licenseId": None})
    )
    return respx.get("https://data.example.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(200, json=rows)
    )


@respx.mock
def test_a_dataset_answers_its_records_as_tier_three_features(tmp_path: Path) -> None:
    data = _socrata_routes([ROW])
    budget = Budget()
    with SqliteCache(tmp_path / "c.sqlite") as cache:
        result = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache, budget))
        again = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache, budget))
    assert result.answered and len(result.features) == 1
    assert result.vintage == "socrata-2026-10-27"
    assert result.source_url == "https://data.example.gov/d/abcd-1234"
    assert again.features == result.features
    assert data.call_count == 1, "the second ask is the cache's"
    assert budget.api_calls_used == 2, "one metadata read and one page"
    sent = parse_qs(urlparse(str(data.calls[0].request.url)).query)
    assert sent["$order"] == [":id"] and "intersects(the_geom" in sent["$where"][0]


@respx.mock
def test_a_cassette_replays_offline(tmp_path: Path) -> None:
    _socrata_routes([ROW])
    with SqliteCache(tmp_path / "c.sqlite") as cache:
        recorded = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    respx.clear()
    with SqliteCache(tmp_path / "c.sqlite", offline=True) as cache:
        replayed = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
        missed = fetch_portal(_entry(), CORRIDOR, date(2026, 10, 29), _ctx(cache))
    assert replayed.features == recorded.features
    assert missed.reason == "not in the cassette"


@respx.mock
def test_a_stale_dataset_answers_nothing_and_is_not_asked() -> None:
    data = _socrata_routes([ROW], updated=date(2026, 9, 1))
    with SqliteCache() as cache:
        result = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert not result.answered and "last updated 2026-09-01" in (result.reason or "")
    assert data.call_count == 0


@respx.mock
def test_a_dataset_that_does_not_say_when_it_changed_is_refused() -> None:
    respx.get("https://data.example.gov/api/views/abcd-1234.json").mock(
        return_value=httpx.Response(200, json={"name": "no stamp"})
    )
    with SqliteCache() as cache:
        result = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert "does not say when it was last updated" in (result.reason or "")


@respx.mock
def test_pages_are_read_whole_or_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "PAGE_SIZE", 2)
    monkeypatch.setattr(client, "MAX_PAGES", 2)
    data = _socrata_routes([])
    data.side_effect = [
        httpx.Response(200, json=[ROW, ROW]),
        httpx.Response(200, json=[ROW]),
    ]
    with SqliteCache() as cache:
        result = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert len(result.features) == 3
    offsets = [parse_qs(urlparse(str(c.request.url)).query)["$offset"] for c in data.calls]
    assert offsets == [["0"], ["2"]]

    data.side_effect = [httpx.Response(200, json=[ROW, ROW])] * 2
    with SqliteCache() as cache:
        refused = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert refused.features == [] and "refused rather than truncated" in (refused.reason or "")


@respx.mock
def test_a_dataset_whose_every_record_fails_is_a_reason_not_an_empty_answer() -> None:
    _socrata_routes([{"start_date": "2026-10-27T00:00:00"}])
    with SqliteCache() as cache:
        result = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert result.reason == "the dataset returned 1 record(s) and none could be read (no geometry)"

    _socrata_routes([])
    with SqliteCache() as cache:
        empty = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert empty.answered and empty.features == [], "an empty corridor is an answer"


@respx.mock
def test_a_token_is_sent_when_set_and_is_never_part_of_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LONGRUN_SOCRATA_APP_TOKEN", "tok")
    data = _socrata_routes([ROW])
    with SqliteCache(tmp_path / "c.sqlite") as cache:
        fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert data.calls[0].request.headers["X-App-Token"] == "tok"
    monkeypatch.delenv("LONGRUN_SOCRATA_APP_TOKEN")
    with SqliteCache(tmp_path / "c.sqlite", offline=True) as cache:
        assert fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache)).answered


@respx.mock
def test_a_portal_that_is_down_is_a_reason() -> None:
    respx.get("https://data.example.gov/api/views/abcd-1234.json").mock(
        return_value=httpx.Response(503)
    )
    with SqliteCache() as cache:
        result = fetch_portal(_entry(), CORRIDOR, DAY, _ctx(cache))
    assert result.reason is not None and result.reason.startswith("its metadata could not be read")


def test_a_feed_scoped_dataset_needs_no_corridor() -> None:
    entry = _entry(scope="feed")
    assert portal_args(entry.name, None, DAY)["bbox"] is None


# --- ArcGIS -----------------------------------------------------------------


def test_arcgis_asks_for_the_window_in_its_own_dialect() -> None:
    assert arcgis.where(_arcgis(where="Type = 'Closure'"), DAY) == (
        "(Type = 'Closure') AND "
        "(EndDate IS NULL OR EndDate >= TIMESTAMP '2026-10-27 00:00:00') AND "
        "(StartDate IS NULL OR StartDate <= TIMESTAMP '2026-10-29 23:59:59')"
    )
    assert arcgis.where(_arcgis(window_filter=False), DAY) == "1=1"


LAYER = "https://services.example.com/abc/arcgis/rest/services/Closures/FeatureServer/0"


def _feature(i: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": LINE,
        "properties": {"OBJECTID": i, "StartDate": 1782864000000, "EndDate": 1782950400000},
    }


@respx.mock
def test_arcgis_pages_while_the_server_says_it_held_records_back() -> None:
    respx.get(LAYER).mock(
        return_value=httpx.Response(
            200, json={"editingInfo": {"lastEditDate": _epoch(date(2026, 10, 27)) * 1000}}
        )
    )
    query = respx.get(f"{LAYER}/query").mock(
        side_effect=[
            httpx.Response(200, json={"features": [_feature(1)], "exceededTransferLimit": True}),
            httpx.Response(
                200,
                json={"features": [_feature(2)], "properties": {"exceededTransferLimit": False}},
            ),
        ]
    )
    with SqliteCache() as cache:
        result = fetch_portal(_arcgis(), CORRIDOR, date(2026, 7, 1), _ctx(cache))
    assert [f.ref for f in result.features] == [None, None]
    assert len(result.features) == 2
    assert result.vintage == "arcgis-2026-10-27"
    first = parse_qs(urlparse(str(query.calls[0].request.url)).query)
    assert first["f"] == ["geojson"] and first["outSR"] == ["4326"]
    assert first["geometry"] == ["-94.59,39.09,-94.57,39.11"]
    assert first["geometryType"] == ["esriGeometryEnvelope"]
    second = parse_qs(urlparse(str(query.calls[1].request.url)).query)
    assert second["resultOffset"] == ["1"]


@respx.mock
def test_an_arcgis_error_object_is_a_reason_not_an_empty_answer() -> None:
    respx.get(LAYER).mock(
        return_value=httpx.Response(200, json={"editingInfo": {"lastEditDate": 1793000000000}})
    )
    respx.get(f"{LAYER}/query").mock(
        return_value=httpx.Response(200, json={"error": {"code": 400, "message": "Invalid query"}})
    )
    with SqliteCache() as cache:
        result = fetch_portal(_arcgis(), CORRIDOR, DAY, _ctx(cache))
    assert result.reason == "RuntimeError: ArcGIS error 400: Invalid query"


def test_arcgis_rows_carry_their_geometry_beside_their_attributes() -> None:
    rows = arcgis.rows({"features": [_feature(7), "junk"]})
    assert rows == [
        {"OBJECTID": 7, "StartDate": 1782864000000, "EndDate": 1782950400000, "geometry": LINE}
    ]
    assert arcgis.updated({"editingInfo": {"dataLastEditDate": 1793000000000}}) == "2026-10-26"
    assert arcgis.updated({}) is None


@respx.mock
def test_a_map_service_without_edit_dates_is_dated_by_its_newest_record() -> None:
    """Phoenix's map service publishes no `editingInfo`. The row names a column whose
    newest value stands in, so the layer can still be told from one nobody maintains."""
    respx.get(LAYER).mock(return_value=httpx.Response(200, json={"name": "Restrictions"}))

    def query(request: httpx.Request) -> httpx.Response:
        if "outStatistics" in request.url.params:
            newest = {"features": [{"attributes": {"newest": "2026-10-27"}}]}
            return httpx.Response(200, json=newest)
        return httpx.Response(200, json={"features": []})

    respx.get(f"{LAYER}/query").mock(side_effect=query)
    with SqliteCache() as cache:
        dated = fetch_portal(_arcgis(freshness_field="StartDate"), CORRIDOR, DAY, _ctx(cache))
    with SqliteCache() as cache:
        undated = fetch_portal(_arcgis(), CORRIDOR, DAY, _ctx(cache))
    assert dated.answered and dated.vintage == "arcgis-2026-10-27"
    assert "does not say when it was last updated" in (undated.reason or "")
