"""The sixteen WZDx feeds M14 adopted, and the rule that decided which may be committed.

`test_wzdx.py` is about the *parser*: three feeds pinned at three spec versions, chosen to
demonstrate that one reader covers 4.0 through 4.2. This file is about the *set*. It asks
whether every adapter this project registers is well formed, licensed and reachable, and
it makes ADR 0038's licence rule executable rather than a paragraph somebody has to
remember.

**The rule, as a test:** a cassette may be committed only for a feed whose own
`feed_info.license` declares CC0. `test_every_committed_cassette_declares_cc0` scans the
directory rather than a list, so a cassette added later without a licence fails here
instead of being noticed by nobody. That is the executable half of ADR 0006's "a source
that cannot be redistributed cannot back a committed cassette".

Every feature count, spec version and licence finding quoted in the adapter docstrings
came from a live call on 2026-09-23. The live checks at the bottom are `network`-marked
and skipped by the gate; what runs in CI is the recorded half.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from longrun.adapters.base import AdapterContext, valid_jurisdiction_id
from longrun.adapters.wzdx import (
    austin,
    deldot,
    iddot,
    indot,
    iowadot,
    kytc,
    ladotd,
    mdotsha,
    msdot,
    ncdot,
    nddot,
    necdot,
    njdot,
    nysdot,
    wisdot,
    wsdot,
)
from longrun.adapters.wzdx.feed import feed_publisher, feed_version, parse_wzdx
from longrun.core.data.cache import SqliteCache
from longrun.core.models.context import Budget

pytestmark = pytest.mark.contract

CASSETTES = Path(__file__).parent / "cassettes"

#: The CC0 URL, in both the forms publishers write it. Maricopa omits the trailing slash.
CC0 = "https://creativecommons.org/publicdomain/zero/1.0"

#: Feeds that declare CC0 in their own envelope, so ADR 0038 permits a cassette. Version
#: and publisher are pinned so a re-record that quietly changed either is visible.
COMMITTABLE = [
    ("nddot", "4.0", "NDDOT", nddot.CLOSURES),
    ("deldot", "4.1", "HaulHub Technologies", deldot.CLOSURES),
    ("indot", "4.0", "INDOTCastleRock", indot.CLOSURES),
    ("kytc", "4.1", "Kentucky Transportation Cabinet (KYTC)", kytc.CLOSURES),
    ("ladotd", "4.1", "HaulHub Technologies", ladotd.CLOSURES),
    ("mdotsha", "4.1", "Maryland DOT SHA", mdotsha.CLOSURES),
    ("msdot", "4.2", "Mississippi Department of Transportation", msdot.CLOSURES),
    ("njdot", "4.1", "NJIT", njdot.CLOSURES),
    ("wisdot", "4.2", "Work Zone Manager", wisdot.CLOSURES),
    ("austin", "4.2", "City of Austin", austin.CLOSURES),
]

#: Feeds that declare no licence. Live, adopted, and deliberately *without* a cassette -
#: which is the point of naming them here rather than leaving the absence implicit.
UNCOMMITTABLE = [
    ("necdot", necdot.CLOSURES),
    ("iddot", iddot.CLOSURES),
    ("iowadot", iowadot.CLOSURES),
    ("nysdot", nysdot.CLOSURES),
    ("ncdot", ncdot.CLOSURES),
    ("wsdot", wsdot.CLOSURES),
]

ADOPTED = [(s, a) for s, _, _, a in COMMITTABLE] + UNCOMMITTABLE

#: Live URLs, for the `network`-marked half.
URLS = {
    "nddot": nddot.URL,
    "deldot": deldot.URL,
    "indot": indot.URL,
    "kytc": kytc.URL,
    "ladotd": ladotd.URL,
    "mdotsha": mdotsha.URL,
    "msdot": msdot.URL,
    "njdot": njdot.URL,
    "wisdot": wisdot.URL,
    "austin": austin.URL,
    "necdot": necdot.URL,
    "iddot": iddot.URL,
    "iowadot": iowadot.URL,
    "nysdot": nysdot.URL,
    "ncdot": ncdot.URL,
    "wsdot": wsdot.URL,
}


def _payload(short: str) -> dict[str, Any]:
    return json.loads((CASSETTES / f"wzdx_{short}.json").read_text(encoding="utf-8"))


def _licence_of(payload: dict[str, Any]) -> str | None:
    info = payload.get("feed_info") or payload.get("road_event_feed_info") or {}
    value = info.get("license")
    return str(value) if value is not None else None


# --- ADR 0038, as something that runs ---------------------------------------


def test_every_committed_cassette_declares_cc0() -> None:
    """The rule, applied to the directory rather than to a list.

    ADR 0006 says a source that cannot be redistributed cannot back a committed cassette,
    and ADR 0038 says the only redistribution grant this project acts on is the one the
    publisher ships inside the payload. A cassette is a redistribution of somebody's feed
    into a public repository, so it needs that grant and nothing weaker.

    Scanning the directory rather than iterating `COMMITTABLE` is deliberate: a list can be
    extended without thinking, a directory cannot be added to without this failing.
    """
    for path in sorted(CASSETTES.glob("wzdx_*.json")):
        licence = _licence_of(json.loads(path.read_text(encoding="utf-8")))
        assert licence is not None, f"{path.name} declares no licence and must not be committed"
        assert licence.rstrip("/") == CC0, (
            f"{path.name} declares {licence!r}, which is not the CC0 dedication ADR 0038 "
            f"requires of a committed cassette"
        )


@pytest.mark.parametrize(("short", "adapter"), UNCOMMITTABLE, ids=[u[0] for u in UNCOMMITTABLE])
def test_a_feed_with_no_declared_licence_has_no_cassette(short: str, adapter: Any) -> None:
    """The other half of the rule, and the one with no natural symptom.

    An adapter with no cassette looks exactly like an adapter whose cassette nobody has got
    round to recording. These six are the first kind. 511NY is the case that shows the
    difference matters: its terms prohibit republishing any part of the feed, so a cassette
    would not be a missing test, it would be a licence breach shipped in git history.
    """
    assert not (CASSETTES / f"wzdx_{short}.json").exists(), (
        f"{short} declares no licence; ADR 0038 forbids committing its payload"
    )


# --- the recorded payloads ---------------------------------------------------


@pytest.mark.parametrize(
    ("short", "version", "publisher", "adapter"), COMMITTABLE, ids=[c[0] for c in COMMITTABLE]
)
def test_each_committed_feed_parses_at_its_recorded_version(
    short: str, version: str, publisher: str, adapter: Any
) -> None:
    payload = _payload(short)
    assert feed_version(payload) == version
    assert feed_publisher(payload) == publisher
    features = parse_wzdx(payload, source_url=adapter.name)
    assert features, f"{short} parsed to nothing"
    assert all(f.tier == 1 and f.kind == "closures" for f in features)
    assert all(f.detail for f in features), f"{short} lost its descriptions"


def test_the_envelope_key_does_not_follow_the_spec_version() -> None:
    """The correction M14 made to `feed.py`'s account of `ENVELOPE_KEYS`.

    That module explains the two keys through Kansas: 4.1 renamed `road_event_feed_info`
    to `feed_info` and "Kansas has not moved", which reads as though the old key means an
    old feed. It does not. North Dakota publishes **4.0 under the new key** while Indiana
    publishes 4.0 under the old one, so looking in both places is load-bearing at every
    version rather than a compatibility shim for 4.0.
    """
    assert feed_version(_payload("nddot")) == "4.0"
    assert "feed_info" in _payload("nddot")
    assert feed_version(_payload("indot")) == "4.0"
    assert "road_event_feed_info" in _payload("indot")


def test_new_jersey_states_that_every_lane_is_open_rather_than_declining_to_say() -> None:
    """A finding about the publisher, in the shape `test_wzdx.py` uses for Maricopa.

    MCDOT publishes `vehicle_impact: "unknown"`, which `feed.UNINFORMATIVE` turns into a
    fallback on the event type - a publisher declining to say. NJIT publishes
    `all-lanes-open`, which is a publisher saying. Both end with no New Jersey and no
    Phoenix work zone clearing ADR 0013's gate 1 on the impact field, and the reasons are
    opposite; a sheet reporting "740 work zones, none blocking" is true of New Jersey and
    would be an invention about Phoenix.
    """
    features = parse_wzdx(_payload("njdot"))
    assert {f.category for f in features} == {"all-lanes-open"}


def test_a_fully_closed_road_still_reaches_gate_one_in_the_new_feeds() -> None:
    """ADR 0013 gate 1 reads `Feature.category`, so `all-lanes-closed` has to survive the
    parse for any of these sixteen feeds to be able to fail a route rather than only
    annotate one. Austin is the cassette that carries such a record."""
    from longrun.core.scorers.closures import blocks_pedestrians

    closed = [f for f in parse_wzdx(_payload("austin")) if f.category == "all-lanes-closed"]
    assert closed, "the Austin cassette carries no fully-closed record to test with"
    assert all(blocks_pedestrians(f) for f in closed)


# --- the adapters themselves -------------------------------------------------


@pytest.mark.parametrize(("short", "adapter"), ADOPTED, ids=[a[0] for a in ADOPTED])
def test_every_adopted_adapter_is_well_formed_and_licensed(short: str, adapter: Any) -> None:
    from longrun.adapters.base import Adapter
    from longrun.core.export.attribution import licence_for

    assert isinstance(adapter, Adapter)
    assert adapter.tier == 1, "scope 7.10 assigns tier 1 to every WZDx feed"
    assert adapter.kind == "closures"
    assert adapter.jurisdictions
    assert all(valid_jurisdiction_id(i) for i in adapter.jurisdictions)
    assert licence_for(adapter.source) is not None, adapter.source


@pytest.mark.parametrize(("short", "adapter"), ADOPTED, ids=[a[0] for a in ADOPTED])
def test_an_adopted_adapter_offline_is_a_reason_and_costs_nothing(short: str, adapter: Any) -> None:
    """Sixteen new ways for `longrun repair` to raise, if any of them raised.

    `base.Adapter` says an adapter never raises: a feed that is down, a schema that
    drifted and a jurisdiction with nothing to report are all `AdapterResult`s carrying a
    reason. Offline is the cheapest of those to reproduce, and it also pins that a plan
    replaying a cassette spends no budget - `cache.get` raises `CacheMiss` before the
    producer runs, so the call never reaches `spend_api_call`.

    Offline is set on the **cache**, not through `LONGRUN_OFFLINE`. The environment
    variable is the CLI's door to the same setting and `conftest` clears every `LONGRUN_*`
    var per test, so a test that reached for the variable would silently go to the network
    - which the first draft of this one did, and the suite's connection guard caught.
    """
    from datetime import date

    with SqliteCache(":memory:", offline=True) as cache:
        ctx = AdapterContext(cache=cache, budget=Budget(), offline=True)
        result = adapter.fetch(None, date(2026, 9, 23), ctx)
    assert not result.answered
    assert result.reason == "not in the cassette"
    assert result.source_url
    assert ctx.budget.api_calls_used == 0


def test_one_statewide_fetch_answers_for_every_place_inside_the_state() -> None:
    """The budget story, which is the whole reason sixteen adapters is affordable.

    `MAX_ADAPTER_FETCHES` is 16 per kind and these are whole statewide files - none of
    the endpoints accepts a spatial filter, so `client.fetch_feed` pulls the lot. That is
    only sane because the registry fans out **by adapter, not by jurisdiction**: a
    Wisconsin route crossing a county and eleven incorporated places is one request, not
    twelve, and adding a state to the set adds nothing to any plan outside it.
    """
    from longrun.adapters.registry import AdapterRegistry
    from longrun.core.models.jurisdiction import Jurisdiction

    inside = ("tiger:county:55025", "tiger:state:55")
    crossed = [
        Jurisdiction(id="tiger:county:55025", level="county", name="Dane", within=inside[1:])
    ] + [
        Jurisdiction(id=f"tiger:place:55{n:05d}", level="place", name=f"Place {n}", within=inside)
        for n in range(1, 12)
    ]
    with SqliteCache(offline=True) as cache:
        registry = AdapterRegistry(cache, Budget(), adapters=[wisdot.CLOSURES])
        found = registry.fetch("closures", crossed, None, date(2026, 9, 23))
    assert registry._fetches == {"closures": 1}, "one feed, one fetch, twelve jurisdictions"
    assert all(
        [a.adapter for a in answer.attempts if a.tier == 1] == ["wzdx.wisdot"]
        for answer in found.answers
    ), "every jurisdiction it covered has to record it, or the sheet implies twelve requests"


def test_no_two_adapters_claim_the_same_state() -> None:
    """Two feeds over one jurisdiction is legitimate - Arizona has two and
    `test_keyed_adapters` explains why - but it must be a decision. Arizona is the only
    place this project has made it, so any second one here is an accident.
    """
    from collections import Counter

    from longrun.adapters.registry import discover

    found, failures = discover()
    assert not failures, failures
    counts = Counter(i for a in found if a.kind == "closures" for i in a.jurisdictions)
    doubled = {i for i, n in counts.items() if n > 1}
    assert doubled == set(), f"more than one closure adapter claims {sorted(doubled)}"


def test_every_m14_adapter_is_reachable_through_entry_points() -> None:
    """By discovery, not by import - which is how a plan reaches them. An adapter written,
    licensed and never added to `pyproject.toml` is invisible in exactly the way
    `test_keyed_adapters.py:324` exists to prevent for keys."""
    from longrun.adapters.registry import discover

    found, failures = discover()
    assert not failures, failures
    assert {a.name for a in found} >= {a.name for _, a in ADOPTED}


def test_the_adapter_set_covers_the_states_m14_claimed() -> None:
    """The headline, pinned. Seventeen states and one city gained a tier-1 closure adapter,
    and the list is written down so a silently dropped entry point is a failure rather than
    a quieter coverage report."""
    from longrun.adapters.registry import discover

    found, _ = discover()
    states = {i for a in found if a.kind == "closures" for i in a.jurisdictions}
    expected = {
        f"tiger:state:{fips}"
        for fips in (
            "04",  # Arizona, before M14
            "10",  # Delaware
            "16",  # Idaho
            "18",  # Indiana
            "19",  # Iowa
            "20",  # Kansas, before M14
            "21",  # Kentucky
            "22",  # Louisiana
            "23",  # Maine
            "24",  # Maryland
            "25",  # Massachusetts, before M14 (keyed)
            "28",  # Mississippi
            "29",  # Missouri, before M14
            "33",  # New Hampshire
            "34",  # New Jersey
            "36",  # New York
            "37",  # North Carolina
            "38",  # North Dakota
            "50",  # Vermont
            "53",  # Washington
            "55",  # Wisconsin
        )
    }
    assert expected <= states
    assert austin.AUSTIN in states


# --- live, and separate ------------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize(("short", "adapter"), ADOPTED, ids=[a[0] for a in ADOPTED])
def test_the_live_feed_still_answers_and_still_parses(short: str, adapter: Any) -> None:
    """What a cassette cannot tell you, and what six of these sixteen have instead of one.

    `massdot.py:28`: a feed that answers and parses to nothing would look exactly like a
    state with no work zones. So a 200 is not the assertion - parsed features are.
    """
    import httpx

    try:
        response = httpx.get(URLS[short], timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - an unreachable feed is a skip, not a failure
        pytest.skip(f"{short} unreachable: {exc}")

    assert parse_wzdx(payload), f"{short} answered and parsed to nothing"
    assert feed_version(payload), f"{short} carries no WZDx envelope"


@pytest.mark.network
@pytest.mark.parametrize(
    ("short", "version", "publisher", "adapter"), COMMITTABLE, ids=[c[0] for c in COMMITTABLE]
)
def test_a_committed_feed_still_declares_the_licence_it_was_committed_under(
    short: str, version: str, publisher: str, adapter: Any
) -> None:
    """A publisher may withdraw a CC0 dedication from future data. If one does, the
    cassette recorded under it stays lawful and the *next* recording is not - so this is
    the check that has to be live, and it is the one that would otherwise be made by
    nobody."""
    import httpx

    try:
        response = httpx.get(URLS[short], timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{short} unreachable: {exc}")

    licence = _licence_of(payload)
    assert licence is not None and licence.rstrip("/") == CC0, (
        f"{short} now declares {licence!r}; its cassette may no longer be re-recorded"
    )
