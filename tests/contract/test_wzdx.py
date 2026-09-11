"""WZDx adapters against recorded responses (scope 7.10, 11).

**The `contract` marker's first users.** It has existed since M0 and selected nothing: all
eight files in this directory are `network`-marked and deselected by the gate, so "adapter
contract tests" has meant "tests that do not run in CI". These run in CI, off committed
payloads, which is what `tests/contract/README.md` describes.

The cassettes are real responses, trimmed to a handful of features each, and they are
committed because all three feeds are CC0 - ADR 0006's rule that a non-redistributable
source cannot back a golden or a cassette is what decided which feeds this project uses at
all.

The README names the cases that matter: *"including the failure cases (feed down, schema
drift, empty result) - those are the ones that decide whether the coverage manifest tells
the truth."* Each has a test here. The live-network check is separate and `network`-marked.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from longrun.adapters.base import AdapterContext
from longrun.adapters.wzdx import kdot, maricopa, modot
from longrun.adapters.wzdx.client import fetch_feed, wzdx_args
from longrun.adapters.wzdx.feed import (
    feed_publisher,
    feed_version,
    parse_timestamp,
    parse_wzdx,
)
from longrun.core.data.cache import SqliteCache, args_hash
from longrun.core.models.context import Budget

pytestmark = pytest.mark.contract

CASSETTES = Path(__file__).parent / "cassettes"
DAY = date(2026, 9, 15)

#: Version, publisher and adapter, as recorded. Pinned so a re-record that quietly changed
#: publisher is visible rather than absorbed.
FEEDS = [
    ("kdot", "4.0", kdot.CLOSURES),
    ("modot", "4.1", modot.CLOSURES),
    ("maricopa", "4.2", maricopa.CLOSURES),
]


def _payload(short: str) -> dict[str, Any]:
    return json.loads((CASSETTES / f"wzdx_{short}.json").read_text(encoding="utf-8"))


def _ctx(cache: Any) -> AdapterContext:
    return AdapterContext(cache=cache, budget=Budget(), offline=True)


# --- the recorded payloads --------------------------------------------------


@pytest.mark.parametrize(("short", "version", "adapter"), FEEDS, ids=[f[0] for f in FEEDS])
def test_each_feed_parses_at_its_own_spec_version(short: str, version: str, adapter: Any) -> None:
    """One parser, three spec versions. This is the claim `feed.py` exists to make good."""
    payload = _payload(short)
    assert feed_version(payload) == version
    features = parse_wzdx(payload)
    assert features, f"{short} parsed to nothing"
    assert all(f.tier == 1 for f in features)
    assert all(f.kind == "closures" for f in features)


def test_the_four_zero_feed_uses_the_old_envelope_key() -> None:
    """The bug this caught. 4.1 renamed `road_event_feed_info` to `feed_info`; Kansas still
    publishes the old one, so a reader that knew only the new name declared a live
    480-feature feed to have no envelope and reported a whole state unavailable."""
    payload = _payload("kdot")
    assert "feed_info" not in payload
    assert "road_event_feed_info" in payload
    assert feed_version(payload) == "4.0"
    assert feed_publisher(payload) == "KDOTCastleRock"


def test_core_details_is_nested_at_every_version_this_project_reads() -> None:
    """Written to demonstrate that 4.0 flattens what 4.1 nests, and it demonstrated the
    opposite: `core_details` was introduced in 4.0, so Kansas nests exactly like Missouri.

    Kept as the record of that, because `_detail`'s two-place lookup now reads as defensive
    rather than as the reason one parser covers three versions - and a comment claiming a
    structural difference that does not exist would send the next reader looking for it.
    """
    for short, _, _ in FEEDS:
        properties = _payload(short)["features"][0]["properties"]
        assert "core_details" in properties, f"{short} flattens after all - update _detail"
        assert "road_names" in properties["core_details"]
    assert parse_wzdx(_payload("kdot"))[0].detail, "the description did not survive"


@pytest.mark.parametrize("short", ["kdot", "modot"], ids=["kdot", "modot"])
def test_a_fully_closed_road_is_categorised_so_gate_one_can_see_it(short: str) -> None:
    """ADR 0013 gate 1 reads `Feature.category`, so `all-lanes-closed` has to arrive there
    intact or no closure can ever hard-flag."""
    from longrun.core.scorers.closures import blocks_pedestrians

    features = parse_wzdx(_payload(short))
    closed = [f for f in features if f.category == "all-lanes-closed"]
    assert closed, f"{short}'s cassette carries no fully-closed record to test with"
    assert all(blocks_pedestrians(f) for f in closed)


@pytest.mark.parametrize("short", ["kdot", "modot"], ids=["kdot", "modot"])
def test_a_partly_closed_road_does_not_block_pedestrians(short: str) -> None:
    """Most of every feed. Treating these as impassable would flag nearly every route,
    which is indistinguishable from flagging none."""
    from longrun.core.scorers.closures import blocks_pedestrians

    partial = [f for f in parse_wzdx(_payload(short)) if f.category == "some-lanes-closed"]
    assert partial, f"{short}'s cassette carries no partly-closed record"
    assert not any(blocks_pedestrians(f) for f in partial)


def test_maricopa_declines_to_state_impact_and_that_is_reported_not_guessed() -> None:
    """A finding about the publisher, not the parser. MCDOT publishes
    `vehicle_impact: "unknown"` on every one of its 2,628 features, so no Phoenix work zone
    clears gate 1 on that field. Recorded as a test because it is the sort of fact a later
    reader would otherwise rediscover by wondering why Phoenix never flags."""
    from longrun.core.scorers.closures import blocks_pedestrians

    payload = _payload("maricopa")
    impacts = {(f["properties"] or {}).get("vehicle_impact") for f in payload["features"]}
    assert impacts == {"unknown"}
    features = parse_wzdx(payload)
    assert all(f.category == "work-zone" for f in features), (
        "an uninformative impact must fall back to the event type, not become the category "
        "'unknown' - which reads like a classification rather than a refusal"
    )
    assert not any(blocks_pedestrians(f) for f in features)


def test_every_parsed_feature_carries_prose_a_reader_can_act_on() -> None:
    """The flag detail, and the only signal left where `vehicle_impact` says nothing."""
    for short, _, _ in FEEDS:
        features = parse_wzdx(_payload(short))
        assert all(f.detail for f in features), f"{short} lost its descriptions"


def test_timestamps_survive_every_format_the_feeds_use() -> None:
    """Maricopa emits seven fractional digits with a numeric offset; others use `Z`."""
    assert parse_timestamp("2024-10-22T06:59:00.0000000-07:00") is not None
    assert parse_timestamp("2026-09-15T12:00:00Z") is not None
    assert parse_timestamp("2026-09-15T12:00:00+00:00") is not None
    assert parse_timestamp("") is None
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


def test_a_parsed_time_is_naive_so_it_can_be_compared_with_an_eta() -> None:
    """ETAs are naive local times throughout `core/`; comparing one against an aware
    datetime raises, and `Feature.active_at` does exactly that comparison."""
    parsed = parse_timestamp("2024-10-22T06:59:00.0000000-07:00")
    assert parsed is not None and parsed.tzinfo is None


# --- the failure cases the README names -------------------------------------


def test_an_empty_result_is_an_answer_not_a_failure() -> None:
    """A feed read with nothing in it is evidence. The registry turns this into
    `checked=True` with no features, which is what lets check 6 pass honestly."""
    with SqliteCache() as cache:
        cache.put(
            "adapter.wzdx.modot",
            args_hash(wzdx_args("wzdx.modot", DAY)),
            DAY.isoformat(),
            {"feed_info": {"version": "4.1"}, "type": "FeatureCollection", "features": []},
        )
        result = fetch_feed(modot.URL, "wzdx.modot", DAY, _ctx(cache))
    assert result.answered and result.features == []
    assert result.vintage == "wzdx-4.1"


def test_schema_drift_drops_the_broken_records_and_keeps_the_rest() -> None:
    """A publisher mid-upgrade must not cost a route every closure in the state."""
    payload = {
        "feed_info": {"version": "4.2"},
        "type": "FeatureCollection",
        "features": [
            {"properties": {"core_details": {"event_type": "work-zone"}}},  # no geometry
            "not a feature at all",
            {"geometry": {"type": "Point", "coordinates": [-94.6, 39.1]}},  # no properties
            {
                "geometry": {"type": "Point", "coordinates": [-94.6, 39.1]},
                "properties": {"vehicle_impact": "all-lanes-closed"},
            },
        ],
    }
    features = parse_wzdx(payload)
    assert len(features) == 1 and features[0].category == "all-lanes-closed"


def test_an_end_before_its_start_is_dropped_rather_than_raised() -> None:
    """`Feature` rejects it, and feeds do publish them."""
    payload = {
        "feed_info": {"version": "4.2"},
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-94.6, 39.1]},
                "properties": {
                    "start_date": "2026-09-20T00:00:00Z",
                    "end_date": "2026-09-10T00:00:00Z",
                },
            }
        ],
    }
    assert parse_wzdx(payload) == []


def test_a_feed_that_is_down_becomes_a_reason() -> None:
    """Offline with nothing recorded is the cassette-miss path, which is also what a dead
    feed looks like from the scorer's side: a reason, never an exception."""
    with SqliteCache(offline=True) as cache:
        result = fetch_feed(modot.URL, "wzdx.modot", DAY, _ctx(cache))
    assert not result.answered
    assert result.reason == "not in the cassette"
    assert "adapter" not in (result.reason or ""), "a reason must not leak the cache key"


def test_a_two_hundred_carrying_something_that_is_not_wzdx_is_not_an_answer() -> None:
    with SqliteCache() as cache:
        cache.put(
            "adapter.wzdx.kdot",
            args_hash(wzdx_args("wzdx.kdot", DAY)),
            DAY.isoformat(),
            {"error": "service unavailable"},
        )
        result = fetch_feed(kdot.URL, "wzdx.kdot", DAY, _ctx(cache))
    assert not result.answered and result.reason


def test_a_cache_hit_costs_no_budget() -> None:
    """Spent inside the producer, as `forecast.py` does, so replaying a cassette is free -
    which is what keeps a golden route inside scope 6.4's three minutes."""
    with SqliteCache() as cache:
        cache.put(
            "adapter.wzdx.kdot",
            args_hash(wzdx_args("wzdx.kdot", DAY)),
            DAY.isoformat(),
            _payload("kdot"),
        )
        ctx = _ctx(cache)
        fetch_feed(kdot.URL, "wzdx.kdot", DAY, ctx)
    assert ctx.budget.api_calls_used == 0


def test_the_cache_key_is_provider_scoped_and_stable() -> None:
    """Two feeds must not share a key, and the hash must not move between machines -
    a cassette recorded on one and replayed on another depends on it."""
    assert wzdx_args("wzdx.modot", DAY) != wzdx_args("wzdx.kdot", DAY)
    assert args_hash(wzdx_args("wzdx.modot", DAY)) == args_hash(
        {"adapter": "wzdx.modot", "day": "2026-09-15"}
    )


def test_the_key_carries_no_coordinates() -> None:
    """The fetch is a whole statewide feed, so a bbox in the key would make two routes in
    one state miss each other's cassette for no reason."""
    assert set(wzdx_args("wzdx.modot", DAY)) == {"adapter", "day"}


# --- the registered adapters ------------------------------------------------


@pytest.mark.parametrize(("short", "version", "adapter"), FEEDS, ids=[f[0] for f in FEEDS])
def test_each_adapter_declares_a_well_formed_jurisdiction(
    short: str, version: str, adapter: Any
) -> None:
    """A typo here is the adapter bug with no symptom: it loads, matches nothing, and every
    jurisdiction reports "no adapter" exactly as it did before."""
    from longrun.adapters.base import valid_jurisdiction_id

    assert adapter.jurisdictions
    assert all(valid_jurisdiction_id(i) for i in adapter.jurisdictions)
    assert adapter.tier == 1 and adapter.kind == "closures"


def test_the_two_kansas_city_adapters_do_not_overlap() -> None:
    """Scope 11 region 4 needs two adapter sets on one route, which only works if each
    answers for its own state and neither claims the other's."""
    assert set(modot.CLOSURES.jurisdictions).isdisjoint(kdot.CLOSURES.jurisdictions)


def test_every_adapter_source_has_a_licence() -> None:
    """Scope 14. The sheet prints `LICENCE NOT RECORDED` otherwise, which is what an unmet
    attribution obligation looks like."""
    from longrun.core.export.attribution import licence_for

    for _, _, adapter in FEEDS:
        assert licence_for(adapter.source) is not None, adapter.source


# --- live, and separate -----------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize(("short", "version", "adapter"), FEEDS, ids=[f[0] for f in FEEDS])
def test_the_live_feed_still_answers_at_the_recorded_version(
    short: str, version: str, adapter: Any
) -> None:
    """What the cassettes cannot tell you: whether the feed is still there.

    A version bump is not a failure - it is the thing this test exists to report, since one
    parser covering 4.0 through 4.2 is a claim with a shelf life.
    """
    import httpx

    try:
        response = httpx.get(_url(short), timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - an unreachable feed is a skip, not a failure
        pytest.skip(f"{short} unreachable: {exc}")

    assert parse_wzdx(payload), f"{short} returned a payload that parsed to nothing"
    live = feed_version(payload)
    assert live == version, (
        f"{short} moved from WZDx {version} to {live}; re-record the cassette and check "
        f"`feed._detail` still finds what it needs"
    )


def _url(short: str) -> str:
    return {"kdot": kdot.URL, "modot": modot.URL, "maricopa": maricopa.URL}[short]
