"""Tier 2: the 511 platforms and state DOT feeds that are not WZDx (M20, ADR 0059).

No 511 platform's payload can be committed - none carries a redistribution grant, and the
`state511` family is deliberately absent from `test_payload_licences.FAMILIES` - so the
parsers here are tested against shape fixtures written from each vendor's documentation
(ADR 0055): `tests/contract/shapes/state511_*.json`.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from shapely.geometry import box

from longrun.adapters.base import AdapterContext
from longrun.adapters.keys import ApiKey
from longrun.adapters.state511 import caltrans_lcs, ibi_v2
from longrun.adapters.state511.client import events_args
from longrun.core.data.cache import SqliteCache, args_hash
from longrun.core.models.context import Budget
from longrun.core.scorers.closures import HARD_FLAG_CONFIDENCE, blocks_pedestrians, is_hard

pytestmark = pytest.mark.contract

SHAPES = Path(__file__).parent / "shapes"
DAY = date(2026, 10, 28)


def _shape(name: str) -> Any:
    return json.loads((SHAPES / name).read_text(encoding="utf-8"))["payload"]


def _by_ref(features: list[Any]) -> dict[str, Any]:
    return {f.ref: f for f in features}


# --- IBI Group's event list ---------------------------------------------------


def test_every_placeable_event_becomes_a_tier_two_closure() -> None:
    features = ibi_v2.parse_events(_shape("state511_ga_events.json"), source_url="x")
    by_ref = _by_ref(features)
    assert set(by_ref) == {"9001", "9002", "9004", "9005", "9006"}
    assert all(f.kind == "closures" and f.tier == 2 for f in features)


def test_an_incident_with_no_planned_end_is_dropped() -> None:
    """A crash nobody has scheduled the end of is no place in a plan for a run."""
    assert "9003" not in _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))


def test_a_drawn_event_is_the_road_and_clears_the_hard_flag_line() -> None:
    event = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))["9001"]
    assert event.geometry["type"] == "LineString"
    assert event.geometry["coordinates"][0] == pytest.approx([-84.3902, 33.7501])
    assert len(event.geometry["coordinates"]) == 4
    assert event.confidence == ibi_v2.DRAWN_CONFIDENCE >= HARD_FLAG_CONFIDENCE


def test_an_event_placed_by_its_ends_is_a_chord_held_below_the_line() -> None:
    event = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))["9002"]
    assert event.geometry == {
        "type": "LineString",
        "coordinates": [[-84.4, 33.8], [-84.401, 33.802]],
    }
    assert event.confidence == ibi_v2.UNDRAWN_CONFIDENCE < HARD_FLAG_CONFIDENCE


def test_an_event_with_one_point_is_a_point() -> None:
    event = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))["9005"]
    assert event.geometry == {"type": "Point", "coordinates": [-84.37, 33.76]}
    assert event.confidence == ibi_v2.UNDRAWN_CONFIDENCE


def test_a_full_closure_is_shut_and_anything_else_is_lanes() -> None:
    by_ref = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))
    assert by_ref["9002"].category == "road-closure" and blocks_pedestrians(by_ref["9002"])
    assert by_ref["9001"].category == "some-lanes-closed"
    assert not blocks_pedestrians(by_ref["9001"])


def test_unix_seconds_are_naive_utc() -> None:
    """ADR 0046: a feature's window is a naive UTC instant. 1793001600 is 08:00 UTC."""
    event = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))["9001"]
    assert event.start == datetime(2026, 10, 26, 8, 0)
    assert event.end == datetime(2026, 11, 2, 8, 0)
    assert event.start.tzinfo is None


def test_a_roadwork_with_no_planned_end_is_kept_open_ended() -> None:
    event = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))["9006"]
    assert event.end is None and event.start is not None


def test_a_recurring_event_is_held_below_the_line_even_when_drawn() -> None:
    """Its window is the campaign - three weeks for a closure that holds 9 PM to 5 AM - and
    the schedule is prose this parser does not read. A daytime run must not be failed by a
    night's closure it never meets."""
    event = _by_ref(ibi_v2.parse_events(_shape("state511_ga_events.json")))["9004"]
    assert event.geometry["type"] == "LineString" and event.category == "road-closure"
    assert event.confidence == ibi_v2.UNDRAWN_CONFIDENCE


def test_only_a_drawn_full_closure_can_fail_a_route() -> None:
    """ADR 0059 in one test: the same full closure, drawn and undrawn, at the same moment
    and lying along the route. Only the drawn one clears ADR 0013's five gates."""
    events = _shape("state511_ga_events.json")
    drawn = copy.deepcopy(next(e for e in events if e["ID"] == 9004))
    drawn["Recurrence"] = drawn["RecurrenceSchedules"] = ""
    undrawn = {**drawn, "ID": 9104, "EncodedPolyline": ""}
    by_ref = _by_ref(ibi_v2.parse_events([drawn, undrawn]))
    when = datetime(2026, 10, 27, 12, 0)
    assert is_hard(by_ref["9004"], parallel=True, when=when)
    assert not is_hard(by_ref["9104"], parallel=True, when=when)


def test_a_polyline_that_will_not_decode_falls_back_to_the_ends() -> None:
    event = {**_shape("state511_ga_events.json")[0], "EncodedPolyline": "cy~lEvmab"}
    (feature,) = ibi_v2.parse_events([event])
    assert feature.geometry["coordinates"] == [[-84.3902, 33.7501], [-84.39001, 33.754]]
    assert feature.confidence == ibi_v2.UNDRAWN_CONFIDENCE


def test_a_bad_record_costs_itself_and_nothing_else() -> None:
    good = _shape("state511_ga_events.json")[0]
    backwards = {**good, "ID": 1, "StartDate": good["PlannedEndDate"], "PlannedEndDate": 1}
    unplaced = {**good, "ID": 2, "EncodedPolyline": "", "Latitude": None, "Longitude": None}
    unplaced["LatitudeSecondary"] = unplaced["LongitudeSecondary"] = None
    features = ibi_v2.parse_events([backwards, unplaced, "not an event", good])
    assert [f.ref for f in features] == ["9001"]


@pytest.mark.parametrize("payload", [None, {}, {"Message": "throttled"}, "[]"])
def test_a_payload_that_is_not_an_event_list_parses_to_nothing(payload: Any) -> None:
    assert ibi_v2.parse_events(payload) == []


# --- one platform, as an adapter ---------------------------------------------

KEY = ApiKey(
    env_var="LONGRUN_TEST511_API_KEY",
    service="a test 511 platform",
    register_at="https://511.example.invalid/developers/doc",
)
PLATFORM = ibi_v2.IbiClosures(
    "state511.test", "https://511.example.invalid", ("tiger:state:13",), "state511_test", KEY
)


def _ctx(cache: Any) -> AdapterContext:
    return AdapterContext(cache=cache, budget=Budget(), offline=True)


def test_a_platform_with_no_key_and_nothing_recorded_says_which_key() -> None:
    with SqliteCache() as cache:
        result = PLATFORM.fetch(None, DAY, _ctx(cache))
    assert result.key_missing and KEY.env_var in (result.reason or "")


def test_a_platform_replays_a_recording_with_no_key() -> None:
    with SqliteCache() as cache:
        cache.put(
            "adapter.state511.test",
            args_hash(events_args("state511.test", DAY)),
            DAY.isoformat(),
            _shape("state511_ga_events.json"),
        )
        ctx = _ctx(cache)
        result = PLATFORM.fetch(None, DAY, ctx)
    assert result.answered and result.vintage == "ibi-v2"
    assert len(result.features) == 5
    assert result.source_url == "https://511.example.invalid/api/v2/get/event"
    assert ctx.budget.api_calls_used == 0


def test_a_platform_is_a_tier_two_adapter_by_source_type() -> None:
    from longrun.adapters.base import Adapter

    assert isinstance(PLATFORM, Adapter)
    assert PLATFORM.tier == 2 and PLATFORM.kind == "closures" and PLATFORM.scope == "feed"


# --- Caltrans' Lane Closure System -------------------------------------------

#: Downtown San Francisco, and downtown Los Angeles.
SAN_FRANCISCO = box(-122.42, 37.77, -122.39, 37.80)
LOS_ANGELES = box(-118.26, 34.04, -118.23, 34.06)


def _lcs() -> dict[str, Any]:
    return _by_ref(caltrans_lcs.parse_lcs(_shape("state511_caltrans_lcs.json"), source_url="x"))


def test_every_closure_that_was_not_cancelled_becomes_a_tier_two_record() -> None:
    by_ref = _lcs()
    assert {ref.split("-")[0] for ref in by_ref} == {
        "INV1",
        "INV2",
        "INV4",
        "INV5",
        "INV6",
        "INV7",
        "INV8",
    }, "INV3 was cancelled (code1022) and is dropped"
    assert all(f.tier == 2 and f.kind == "closures" for f in by_ref.values())
    assert all(f.confidence == caltrans_lcs.CALTRANS_CONFIDENCE for f in by_ref.values())


def test_a_closure_is_placed_by_its_ends_and_never_drawn() -> None:
    """Two points: a chord, or a point where they coincide. Both below the hard-flag line
    (ADR 0059) - a straight line between two ends of a coast road crosses the headland."""
    by_ref = _lcs()
    chord = by_ref["INV1-0001-2026-10-28-21:00:00"]
    assert chord.geometry == {
        "type": "LineString",
        "coordinates": [[-122.41, 37.78], [-122.408, 37.784]],
    }
    ramp = by_ref["INV2-0001-2026-10-29-01:00:00"]
    assert ramp.geometry == {"type": "Point", "coordinates": [-122.4, 37.77]}
    assert caltrans_lcs.CALTRANS_CONFIDENCE < HARD_FLAG_CONFIDENCE


def test_pacific_times_are_naive_utc_read_from_the_epoch() -> None:
    """21:00 PDT on 28 October is 04:00 UTC on the 29th."""
    chord = _lcs()["INV1-0001-2026-10-28-21:00:00"]
    assert (chord.start, chord.end) == (datetime(2026, 10, 29, 4, 0), datetime(2026, 10, 29, 12, 0))


def test_a_time_with_no_epoch_is_read_as_pacific_on_its_own_date() -> None:
    """01:30 on 1 November 2026 happens twice; with no epoch to say which, the first (PDT).
    The end, on the 2nd, is in PST - each time converted at its own date (ADR 0046)."""
    record = _lcs()["INV8-0001-2026-11-01-01:30:00"]
    assert record.start == datetime(2026, 11, 1, 8, 30)
    assert record.end == datetime(2026, 11, 2, 17, 0)


def test_a_closure_picked_up_early_ends_when_it_was_picked_up() -> None:
    record = _lcs()["INV4-0001-2026-10-28-21:00:00"]
    assert record.end == datetime(2026, 10, 29, 10, 0), "picked up at 03:00 PDT, not 05:00"


def test_an_indefinite_closure_has_no_end_rather_than_the_year_2999() -> None:
    record = _lcs()["INV5-0001-2026-10-20-09:00:00"]
    assert record.end is None and record.start == datetime(2026, 10, 20, 16, 0)


def test_closure_types_map_onto_what_the_scorer_reads() -> None:
    by_ref = _lcs()
    assert by_ref["INV1-0001-2026-10-28-21:00:00"].category == "some-lanes-closed"
    assert by_ref["INV2-0001-2026-10-29-01:00:00"].category == "road-closure"
    assert by_ref["INV7-0001-2026-11-02-09:00:00"].category == "one-way-traffic"


def test_a_closed_sidewalk_is_a_closed_sidewalk_and_still_cannot_fail_a_route() -> None:
    """The Lane Type Chart's "Sidewalk" is the footway itself. Reported as shut - and held
    below the line all the same, because a chord cannot say it runs along the route."""
    record = _lcs()["INV6-0001-2026-10-30-22:00:00"]
    assert record.category == "sidewalk-closed" and blocks_pedestrians(record)
    assert not is_hard(record, parallel=True, when=datetime(2026, 10, 31, 6, 0))


def test_the_detail_says_where_and_what_in_the_publishers_words() -> None:
    record = _lcs()["INV1-0001-2026-10-28-21:00:00"]
    assert record.detail == (
        "SR-000 North, Example Ave to Sample St - Pavement Repair; Lane closure; "
        "lanes closed: 1 of 2; Conventional Hwy"
    )


@pytest.mark.parametrize("payload", [None, [], {"data": None}, {"data": ["nope", {}]}])
def test_a_file_that_is_not_a_closure_list_parses_to_nothing(payload: Any) -> None:
    assert caltrans_lcs.parse_lcs(payload) == []


def test_only_the_districts_a_corridor_meets_are_read() -> None:
    assert caltrans_lcs.districts_meeting(SAN_FRANCISCO.bounds) == [4]
    assert caltrans_lcs.districts_meeting(LOS_ANGELES.bounds) == [7]
    assert caltrans_lcs.districts_meeting((-80.0, 25.0, -79.9, 25.1)) == []


def test_every_district_has_a_box_inside_california() -> None:
    assert sorted(caltrans_lcs.DISTRICT_BOXES) == list(range(1, 13))
    for west, south, east, north in caltrans_lcs.DISTRICT_BOXES.values():
        assert -124.5 <= west < east <= -114.1 and 32.5 <= south < north <= 42.1


def _recorded(cache: Any, district: int, payload: Any) -> None:
    cache.put(
        "adapter.state511.caltrans",
        args_hash(caltrans_lcs.lcs_args("state511.caltrans", district, DAY)),
        DAY.isoformat(),
        payload,
    )


def test_caltrans_replays_the_district_a_corridor_meets() -> None:
    with SqliteCache() as cache:
        _recorded(cache, 4, _shape("state511_caltrans_lcs.json"))
        ctx = _ctx(cache)
        result = caltrans_lcs.CLOSURES.fetch(SAN_FRANCISCO, DAY, ctx)
    assert result.answered and result.vintage == "caltrans-lcs"
    assert len(result.features) == 7
    assert result.source_url == caltrans_lcs.URL_TEMPLATE.format(n=4)
    assert ctx.budget.api_calls_used == 0


@respx.mock
def test_a_district_that_is_down_is_a_reason_for_the_whole_corridor() -> None:
    """A corridor across two districts missing one reads exactly like one with fewer
    closures, so the answer is the reason, naming the district."""
    corridor = box(-121.3, 37.0, -121.1, 37.2)  # Districts 4, 5 and 10 meet near here
    assert caltrans_lcs.districts_meeting(corridor.bounds) == [4, 5, 10]
    respx.get(caltrans_lcs.URL_TEMPLATE.format(n=5)).mock(return_value=httpx.Response(503))
    with SqliteCache() as cache:
        _recorded(cache, 4, _shape("state511_caltrans_lcs.json"))
        result = caltrans_lcs.CLOSURES.fetch(corridor, DAY, _ctx(cache))
    assert not result.answered
    assert result.reason == (
        "district 5: FeedRefused: HTTP 503 from cwwp2.dot.ca.gov/data/d5/lcs/lcsStatusD05.json"
    )


def test_no_corridor_is_no_district_to_read() -> None:
    with SqliteCache() as cache:
        result = caltrans_lcs.CLOSURES.fetch(None, DAY, _ctx(cache))
    assert result.reason == caltrans_lcs.NO_DISTRICT_REASON


def test_caltrans_answers_for_california_at_tier_two_with_no_key() -> None:
    from longrun.adapters.base import Adapter, key_of, scope_of
    from longrun.core.export.attribution import licence_for

    adapter = caltrans_lcs.CLOSURES
    assert isinstance(adapter, Adapter)
    assert adapter.jurisdictions == ("tiger:state:06",) and adapter.tier == 2
    assert key_of(adapter) is None
    assert scope_of(adapter) == "polygon", "which files are read depends on the corridor"
    assert licence_for(adapter.source) is not None


def test_caltrans_is_discovered_and_the_bay_area_still_asks_its_own_feed() -> None:
    """Separate claimants climb separate ladders (ADR 0047): 511 SF Bay claims the nine
    counties and Caltrans the state, so a San Francisco route asks both. Measured on
    2026-10-28 they are complements, not substitutes - see `state511/__init__.py`."""
    from longrun.adapters.registry import discover
    from longrun.adapters.wzdx import sfbay

    found, failures = discover()
    assert not failures
    assert "state511.caltrans" in {a.name for a in found}
    assert "tiger:state:06" not in sfbay.CLOSURES.jurisdictions


# --- live --------------------------------------------------------------------


def _paths(node: Any, prefix: str = "") -> set[str]:
    """Every key path in a document, lists collapsed: `data[].lcs.closure.facility`."""
    if isinstance(node, dict):
        out = set()
        for key, value in node.items():
            out.add(f"{prefix}.{key}".lstrip("."))
            out |= _paths(value, f"{prefix}.{key}".lstrip("."))
        return out
    if isinstance(node, list):
        out = set()
        for item in node:
            out |= _paths(item, f"{prefix}[]")
        return out
    return set()


@pytest.mark.network
def test_a_live_caltrans_file_still_has_every_key_the_shape_fixture_does() -> None:
    """ADR 0055's live half: the fixture was written from documentation, and this is what
    notices the day the file and the documentation part ways. District 9, the smallest."""
    from longrun.adapters.http import get_json

    url = caltrans_lcs.URL_TEMPLATE.format(n=9)
    with SqliteCache() as cache:
        live = get_json(url, AdapterContext(cache=cache, budget=Budget()))
    missing = _paths(_shape("state511_caltrans_lcs.json")) - _paths(live)
    assert not missing, sorted(missing)
    west, south, east, north = caltrans_lcs.DISTRICT_BOXES[9]
    for record in caltrans_lcs.parse_lcs(live):
        points = record.geometry["coordinates"]
        lon, lat = points[0] if record.geometry["type"] == "LineString" else points
        assert west <= lon <= east and south <= lat <= north, record.ref
