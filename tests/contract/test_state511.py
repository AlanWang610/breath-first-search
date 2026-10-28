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

import pytest

from longrun.adapters.base import AdapterContext
from longrun.adapters.keys import ApiKey
from longrun.adapters.state511 import ibi_v2
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
