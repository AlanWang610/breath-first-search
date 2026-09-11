"""Adapters for sources this install has no key for (scope 7.10, 12; ADR 0006, ADR 0012).

Every tier-2 and tier-3 source this project can reach needs a free key tied to an email
address. Writing them anyway is the settled pattern here, twice over: ADR 0012 kept the HPMS
seam because *"the measurement is not abandoned, it is blocked on a key"*, and ADR 0006
chose Open-Meteo over AirNow because *"a scorer that can only ever say 'not checked' is
honest and useless"* - while insisting the substitution be said out loud on every plan.

So what is tested here is mostly the shape of a refusal. A keyless adapter must **name the
key**, never raise, never claim tier, and never be mistaken for a jurisdiction that had
nothing to report. The suite runs with `LONGRUN_*` cleared by `conftest`, so the no-key path
is the one CI exercises by construction.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from longrun.adapters.base import AdapterContext
from longrun.adapters.keys import ALL_KEYS, AZ_511, NPS, SF_BAY_511
from longrun.adapters.portals import nps as nps_portal
from longrun.adapters.state511 import az511, sfbay
from longrun.core.data.cache import SqliteCache, args_hash
from longrun.core.models.context import Budget

pytestmark = pytest.mark.contract

DAY = date(2026, 9, 15)
CASSETTES = Path(__file__).parent / "cassettes"

KEYED = [
    ("sfbay", sfbay.CLOSURES, SF_BAY_511),
    ("az511", az511.CLOSURES, AZ_511),
    ("nps", nps_portal.TRAIL_STATUS, NPS),
]


def _ctx(cache: Any) -> AdapterContext:
    return AdapterContext(cache=cache, budget=Budget(), offline=True)


# --- the refusal ------------------------------------------------------------


@pytest.mark.parametrize(("short", "adapter", "key"), KEYED, ids=[k[0] for k in KEYED])
def test_a_missing_key_is_a_reason_not_an_exception(short: str, adapter: Any, key: Any) -> None:
    with SqliteCache() as cache:
        result = adapter.fetch(None, DAY, _ctx(cache))
    assert not result.answered
    assert result.reason is not None


@pytest.mark.parametrize(("short", "adapter", "key"), KEYED, ids=[k[0] for k in KEYED])
def test_the_reason_names_the_key_and_where_to_get_it(short: str, adapter: Any, key: Any) -> None:
    """ "closures unavailable" tells a reader nothing they can act on. The env var and the
    registration URL together tell them exactly what to do, and a key dropped into `.env`
    later costs no code change."""
    with SqliteCache() as cache:
        reason = adapter.fetch(None, DAY, _ctx(cache)).reason or ""
    assert key.env_var in reason
    assert "http" in reason, "a reader owed a key is owed the URL for it"


@pytest.mark.parametrize(("short", "adapter", "key"), KEYED, ids=[k[0] for k in KEYED])
def test_a_keyless_adapter_spends_no_budget(short: str, adapter: Any, key: Any) -> None:
    """It must not reach the network to discover it cannot authenticate."""
    with SqliteCache() as cache:
        ctx = _ctx(cache)
        adapter.fetch(None, DAY, ctx)
    assert ctx.budget.api_calls_used == 0


def test_a_keyless_jurisdiction_is_never_reported_as_having_nothing(tmp_path: Any) -> None:
    """The failure this milestone exists to prevent, at the registry level: an adapter that
    could not authenticate must reach the manifest as `checked=False`, not as a county with
    no closures."""
    from longrun.adapters.registry import AdapterRegistry
    from longrun.core.models.jurisdiction import Jurisdiction

    arizona = Jurisdiction(id="tiger:state:04", level="state", name="Arizona")
    with SqliteCache() as cache:
        registry = AdapterRegistry(cache, Budget(), adapters=[az511.CLOSURES])
        found = registry.fetch("closures", [arizona], None, DAY)
    answer = found.answers[0]
    assert not answer.checked
    assert answer.tier is None, "an adapter that could not authenticate did not answer"
    assert AZ_511.env_var in (answer.reason or "")


# --- keys are read at call time ---------------------------------------------


def test_a_key_is_read_when_it_is_used_not_when_the_module_loads() -> None:
    """A module-level `os.environ[...]` would make the value a property of when the process
    started. `conftest` clears every `LONGRUN_*` var per test precisely so a suite cannot
    pass or fail on somebody's shell."""
    assert SF_BAY_511.value({}) is None
    assert SF_BAY_511.value({SF_BAY_511.env_var: "abc123"}) == "abc123"


def test_a_blank_or_whitespace_key_counts_as_absent() -> None:
    """`.env.example` ships these blank, and a `.env` copied from it has `KEY=` with nothing
    after it. Treating that as a key produces a 401 instead of an honest reason."""
    assert SF_BAY_511.value({SF_BAY_511.env_var: ""}) is None
    assert SF_BAY_511.value({SF_BAY_511.env_var: "   "}) is None


def test_every_declared_key_appears_blank_in_env_example() -> None:
    """A key the code reads and `.env.example` does not mention is one nobody knows to set.
    Blank, because a committed value in a public repository is a leaked credential."""
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    for key in ALL_KEYS:
        assert key.env_var in text, f"{key.env_var} is read by the code and undocumented"
        line = next(ln for ln in text.splitlines() if ln.startswith(key.env_var))
        value = line.split("=", 1)[1].split("#")[0].strip()
        assert not value, f"{key.env_var} ships with a value: {value!r}"


def test_every_declared_key_names_where_to_register() -> None:
    for key in ALL_KEYS:
        assert key.register_at.startswith("https://")


# --- NPS alerts, which are prose ---------------------------------------------


def test_an_nps_alert_becomes_a_soft_trail_status_feature() -> None:
    """Tier 3 and confidence 0.7: a park's own alerts page is not an LLM reading one, and it
    is not a structured feed either. `MIN_HARD_FLAG_TIER` forbids tier 3 from hard-flagging
    regardless, which is belt and braces on ADR 0013's reasoning about prose."""
    payload = {
        "total": "2",
        "data": [
            {
                "title": "Trail Closure",
                "description": "The river trail is closed for bridge repair.",
                "category": "Closure",
                "url": "https://www.nps.gov/example",
            },
            {
                "title": "Muddy Conditions",
                "description": "Expect mud after recent rain.",
                "category": "Information",
            },
        ],
    }
    features = nps_portal.parse_alerts(payload)
    assert len(features) == 2
    assert all(f.kind == "trail_status" and f.tier == 3 for f in features)
    assert all(f.jurisdiction == "padus:NPS" for f in features)
    assert features[0].detail and "bridge repair" in features[0].detail


def test_an_alert_carries_no_window_rather_than_an_invented_one() -> None:
    """The API publishes `lastIndexedDate` and nothing bounding when an alert applies.
    `None` reads as unbounded, which is right: a standing alert with no stated end is one
    nobody can say has lifted, and inventing a window invents the fact that it expires."""
    features = nps_portal.parse_alerts({"data": [{"title": "Closure", "category": "Closure"}]})
    assert features[0].start is None and features[0].end is None


def test_an_empty_alert_record_is_dropped_not_carried_as_a_blank_flag() -> None:
    payload = {"data": [{"title": "", "description": ""}, "not a record", {"title": "Real"}]}
    assert len(nps_portal.parse_alerts(payload)) == 1


def test_a_payload_that_is_not_an_alert_list_parses_to_nothing() -> None:
    for payload in ({"error": "bad key"}, {"data": "not a list"}, [], None, "html"):
        assert nps_portal.parse_alerts(payload) == []


def test_the_nps_cache_key_is_stable_and_provider_scoped() -> None:
    assert args_hash(nps_portal.nps_args(None, DAY)) == args_hash(
        {"adapter": "portal.nps", "parks": [], "day": "2026-09-15"}
    )
    assert nps_portal.nps_args(["yose"], DAY) != nps_portal.nps_args(["acad"], DAY)


# --- tiers and registration --------------------------------------------------


def test_the_arizona_ladder_prefers_the_keyless_county_feed() -> None:
    """Scope 7.10's ladder, working. A Phoenix route gets MCDOT at tier 1 and never reaches
    AZ511; a Tucson route has nothing at tier 1 and falls to tier 2, where it is told which
    key is missing. That is why `wzdx.maricopa` holds the county and `az511` the state."""
    from longrun.adapters.registry import AdapterRegistry
    from longrun.adapters.wzdx import maricopa
    from longrun.core.models.jurisdiction import Jurisdiction

    phoenix = Jurisdiction(
        id="tiger:place:0455000",
        level="place",
        name="Phoenix",
        within=("tiger:county:04013", "tiger:state:04"),
    )
    tucson = Jurisdiction(
        id="tiger:place:0477000",
        level="place",
        name="Tucson",
        within=("tiger:county:04019", "tiger:state:04"),
    )
    with SqliteCache() as cache:
        registry = AdapterRegistry(cache, Budget(), adapters=[maricopa.CLOSURES, az511.CLOSURES])
        assert [a.name for a in registry.adapters_for("closures", phoenix)] == [
            "wzdx.maricopa",
            "state511.az511",
        ]
        assert [a.name for a in registry.adapters_for("closures", tucson)] == ["state511.az511"]


def test_the_bay_area_adapter_does_not_answer_for_the_whole_state() -> None:
    """MTC's remit is the region; Caltrans' is the state. Claiming `tiger:state:06` would
    have this adapter answer for San Diego."""
    assert "tiger:state:06" not in sfbay.CLOSURES.jurisdictions
    assert "tiger:county:06075" in sfbay.CLOSURES.jurisdictions
    assert len(sfbay.CLOSURES.jurisdictions) == 9


@pytest.mark.parametrize(("short", "adapter", "key"), KEYED, ids=[k[0] for k in KEYED])
def test_every_keyed_adapter_is_well_formed(short: str, adapter: Any, key: Any) -> None:
    from longrun.adapters.base import Adapter, valid_jurisdiction_id
    from longrun.core.export.attribution import licence_for

    assert isinstance(adapter, Adapter)
    assert all(valid_jurisdiction_id(i) for i in adapter.jurisdictions)
    assert licence_for(adapter.source) is not None, adapter.source


def test_every_registered_adapter_is_discovered_and_licensed() -> None:
    """The whole set, through entry points rather than by import - which is how the plan
    reaches them."""
    from longrun.adapters.registry import discover
    from longrun.core.export.attribution import licence_for

    found, failures = discover()
    assert not failures, failures
    assert {a.name for a in found} >= {
        "wzdx.modot",
        "wzdx.kdot",
        "wzdx.maricopa",
        "state511.sfbay",
        "state511.az511",
        "portal.nps",
    }
    assert all(licence_for(a.source) is not None for a in found)


def test_tiers_are_ordered_best_first_after_discovery() -> None:
    """`_plan` relies on it: it takes the first adapter that matches, so the sort is what
    makes "best tier wins" true rather than "whichever entry point loaded first"."""
    from longrun.adapters.registry import discover

    found, _ = discover()
    assert [a.tier for a in found] == sorted(a.tier for a in found)


# --- live, and separate ------------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize(("short", "adapter", "key"), KEYED, ids=[k[0] for k in KEYED])
def test_the_live_endpoint_answers_when_a_key_is_present(
    short: str, adapter: Any, key: Any
) -> None:
    """Skipped unless the key is set, which for a fresh clone is always. This suite will not
    invent a credential or borrow anybody's - the same refusal `test_forecast_providers`
    makes about the NWS user agent."""
    import os

    if not key.value(dict(os.environ)):
        pytest.skip(f"{key.env_var} is not set")

    with SqliteCache() as cache:
        result = adapter.fetch(None, DAY, AdapterContext(cache=cache, budget=Budget()))
    assert result.answered, result.reason
