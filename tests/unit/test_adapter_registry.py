"""The registry, with adapters that exist only in this file (scope 7.10).

Fake in-process adapters rather than real registered ones, deliberately.
`importlib.metadata` reads *installed* metadata, not `pyproject.toml` on disk, so a suite
that discovered its own fixtures would pass or fail depending on whether anybody had
re-synced since the entry-point table last changed - a result nobody can see in the diff.
`discover()` gets its own tests against the real group, which is a different question.

What is worth testing here is the arithmetic of coverage: who gets asked, how often, and
what a jurisdiction reports when the answer came from somebody else's fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from longrun.adapters.base import (
    Adapter,
    AdapterContext,
    AdapterResult,
    valid_jurisdiction_id,
)
from longrun.adapters.extraction.seam import ExtractionRequest, NullExtractor
from longrun.adapters.registry import (
    MAX_ADAPTER_FETCHES,
    MAX_JURISDICTIONS,
    AdapterRegistry,
    discover,
    matches,
)
from longrun.core.data.base import FeatureSource
from longrun.core.data.cache import SqliteCache
from longrun.core.models.context import Budget
from longrun.core.models.features import Feature
from longrun.core.models.jurisdiction import Jurisdiction

DAY = date(2026, 9, 15)

MISSOURI = Jurisdiction(id="tiger:state:29", level="state", name="Missouri")
KANSAS = Jurisdiction(id="tiger:state:20", level="state", name="Kansas")
JACKSON = Jurisdiction(
    id="tiger:county:29095",
    level="county",
    name="Jackson County",
    within=("tiger:state:29",),
)
KCMO = Jurisdiction(
    id="tiger:place:2938000", level="place", name="Kansas City", within=("tiger:state:29",)
)
WYANDOTTE = Jurisdiction(
    id="tiger:county:20209",
    level="county",
    name="Wyandotte County",
    within=("tiger:state:20",),
)


@dataclass
class FakeAdapter:
    """An adapter that records how often it was asked."""

    name: str
    jurisdictions: tuple[str, ...]
    tier: Any = 1
    kind: Any = "closures"
    source: str = "fake"
    result: AdapterResult = field(default_factory=AdapterResult)
    calls: list[Jurisdiction | None] = field(default_factory=list)
    raises: BaseException | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        self.calls.append(ctx.jurisdiction)
        if self.raises is not None:
            raise self.raises
        return self.result


def _feature(**kwargs: Any) -> Feature:
    base: dict[str, Any] = {
        "kind": "closures",
        "category": "lane",
        "geometry": {},
        "tier": 1,
        "confidence": 0.9,
    }
    return Feature(**{**base, **kwargs})


@pytest.fixture
def registry_factory() -> Any:
    def build(*adapters: Adapter, **kwargs: Any) -> AdapterRegistry:
        cache = SqliteCache()
        return AdapterRegistry(cache, Budget(), adapters=list(adapters), **kwargs)

    return build


# --- the protocol -----------------------------------------------------------


def test_the_registry_is_a_feature_source(registry_factory: Any) -> None:
    assert isinstance(registry_factory(), FeatureSource)


# --- matching ---------------------------------------------------------------


def test_a_state_adapter_answers_for_places_inside_it() -> None:
    """The claim that makes one fetch cover twelve jurisdictions."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    for j in (MISSOURI, JACKSON, KCMO):
        assert matches(modot, "closures", j), j.id


def test_a_state_adapter_does_not_answer_across_the_line() -> None:
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    assert not matches(modot, "closures", KANSAS)
    assert not matches(modot, "closures", WYANDOTTE)


def test_matching_is_by_kind_as_well_as_place() -> None:
    """One jurisdiction has different adapters for closures and trail status, and asking
    the wrong one would report a road feed as having answered about a trail."""
    trails = FakeAdapter(name="nps", jurisdictions=("tiger:state:29",), kind="trail_status")
    assert matches(trails, "trail_status", MISSOURI)
    assert not matches(trails, "closures", MISSOURI)


def test_matching_never_uses_a_lexical_prefix() -> None:
    """County `29095` is a prefix of the possible place `2909512`. A `startswith` match
    would hand a county adapter a city it may have nothing to do with."""
    county = FakeAdapter(name="county", jurisdictions=("tiger:county:29095",))
    lookalike = Jurisdiction(id="tiger:place:2909512", level="place", name="Lookalike")
    assert not matches(county, "closures", lookalike)


# --- the fan-out, which is the budget story ---------------------------------


def test_one_statewide_fetch_covers_every_jurisdiction_inside_it(registry_factory: Any) -> None:
    """`alerts.py`'s insight generalised: a WZDx feed is published per state, so asking it
    once per place crossed asks one question many times and pays HTTP for each."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    found = registry_factory(modot).fetch("closures", [MISSOURI, JACKSON, KCMO], None, DAY)
    assert len(modot.calls) == 1, "one feed, one fetch"
    assert len(found.answers) == 3
    assert all(a.checked for a in found.answers)


def test_a_jurisdiction_covered_by_a_coarser_fetch_says_so(registry_factory: Any) -> None:
    """Otherwise a sheet showing three checked jurisdictions implies three requests."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    found = registry_factory(modot).fetch("closures", [MISSOURI, KCMO], None, DAY)
    by_id = {a.jurisdiction: a for a in found.answers}
    assert by_id["tiger:state:29"].covered_by is None, "the state asked for itself"
    assert by_id["tiger:place:2938000"].covered_by == "tiger:state:29"


def test_a_state_line_route_asks_both_states_once_each(registry_factory: Any) -> None:
    """Scope 11 region 4: two DOTs, two adapter sets, one route."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    kdot = FakeAdapter(name="wzdx.kdot", jurisdictions=("tiger:state:20",))
    found = registry_factory(modot, kdot).fetch(
        "closures", [MISSOURI, KANSAS, JACKSON, WYANDOTTE, KCMO], None, DAY
    )
    assert len(modot.calls) == 1 and len(kdot.calls) == 1
    assert all(a.checked for a in found.answers)
    assert {a.adapter for a in found.answers} == {"wzdx.modot", "wzdx.kdot"}


# --- tiers ------------------------------------------------------------------


def test_the_best_tier_answers_and_the_rest_are_not_asked(registry_factory: Any) -> None:
    """`route_forecast`'s NWS -> Open-Meteo ladder. A tier never reached is not a source
    that failed, and recording it as one would misreport what was consulted."""
    good = FakeAdapter(name="tier1", jurisdictions=("tiger:state:29",), tier=1)
    portal = FakeAdapter(name="tier3", jurisdictions=("tiger:state:29",), tier=3)
    found = registry_factory(portal, good).fetch("closures", [MISSOURI], None, DAY)
    assert len(good.calls) == 1 and portal.calls == []
    assert found.answers[0].tier == 1 and found.answers[0].adapter == "tier1"


def test_the_answering_tier_is_recorded_not_the_adapters_best(registry_factory: Any) -> None:
    """Scope 7.6 wants "which tiers returned data", which is only meaningful if the number
    is the one that actually answered."""
    portal = FakeAdapter(name="tier3", jurisdictions=("tiger:state:20",), tier=3)
    found = registry_factory(portal).fetch("closures", [KANSAS], None, DAY)
    assert found.answers[0].tier == 3


def test_adapters_for_lists_claimants_without_fetching(registry_factory: Any) -> None:
    """Scope 13 step 4 asks which adapters exist with no corridor and no budget."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    infos = registry_factory(modot).adapters_for("closures", KCMO)
    assert [i.name for i in infos] == ["wzdx.modot"]
    assert modot.calls == [], "looking up an adapter must not call it"


# --- failure is an answer, not an exception ---------------------------------


def test_an_adapter_that_raises_becomes_a_reason(registry_factory: Any) -> None:
    """Tiers 3 and 4 are brittle by design. A brittle source that could take down a plan
    would make the tiered design worse than having no adapter at all."""
    broken = FakeAdapter(
        name="broken", jurisdictions=("tiger:state:29",), raises=RuntimeError("feed exploded")
    )
    found = registry_factory(broken).fetch("closures", [MISSOURI], None, DAY)
    answer = found.answers[0]
    assert not answer.checked and answer.tier is None
    assert "RuntimeError" in (answer.reason or "") and "feed exploded" in (answer.reason or "")


def test_a_cassette_miss_reads_as_a_cassette_miss(registry_factory: Any) -> None:
    """And carries no args hash: two jurisdictions missing from one cassette are a single
    fact to a reader, and the hash makes them two strings that cannot be deduplicated."""
    from longrun.core.data.cache import CacheMiss

    broken = FakeAdapter(
        name="m",
        jurisdictions=("tiger:state:29",),
        raises=CacheMiss("t", "abc123def456", "2026-09-15"),
    )
    found = registry_factory(broken).fetch("closures", [MISSOURI], None, DAY)
    reason = found.answers[0].reason or ""
    assert reason.startswith("tier 1 m: not in the cassette. Then tier 4 extraction:")
    assert "abc123" not in reason


def test_a_budget_exception_is_named_as_one(registry_factory: Any) -> None:
    from longrun.core.models.context import BudgetExceeded

    broken = FakeAdapter(name="m", jurisdictions=("tiger:state:29",), raises=BudgetExceeded("x"))
    found = registry_factory(broken).fetch("closures", [MISSOURI], None, DAY)
    assert (found.answers[0].reason or "").startswith("tier 1 m: API call budget exhausted.")


def test_a_declared_reason_is_not_a_checked_answer(registry_factory: Any) -> None:
    """An adapter with no key returns a reason rather than raising, and that is still
    `checked=False` - the source was not consulted."""
    keyless = FakeAdapter(
        name="az511",
        jurisdictions=("tiger:state:04",),
        result=AdapterResult(reason="LONGRUN_AZ511_API_KEY is not set"),
    )
    arizona = Jurisdiction(id="tiger:state:04", level="state", name="Arizona")
    found = registry_factory(keyless).fetch("closures", [arizona], None, DAY)
    assert not found.answers[0].checked
    assert (found.answers[0].reason or "").startswith(
        "tier 1 az511: LONGRUN_AZ511_API_KEY is not set. Then tier 4 extraction:"
    )


def test_one_adapter_failing_does_not_stop_another(registry_factory: Any) -> None:
    broken = FakeAdapter(name="mo", jurisdictions=("tiger:state:29",), raises=RuntimeError("down"))
    working = FakeAdapter(
        name="ks",
        jurisdictions=("tiger:state:20",),
        result=AdapterResult(features=[_feature()]),
    )
    found = registry_factory(broken, working).fetch("closures", [MISSOURI, KANSAS], None, DAY)
    by_id = {a.jurisdiction: a for a in found.answers}
    assert not by_id["tiger:state:29"].checked
    assert by_id["tiger:state:20"].checked
    assert len(found.features) == 1


# --- nothing found vs nobody asked ------------------------------------------


def test_a_feed_read_with_nothing_in_it_is_a_checked_answer(registry_factory: Any) -> None:
    """The distinction the whole coverage manifest rests on. An empty feed is evidence;
    an unread feed is not."""
    quiet = FakeAdapter(name="mo", jurisdictions=("tiger:state:29",), result=AdapterResult())
    found = registry_factory(quiet).fetch("closures", [MISSOURI], None, DAY)
    assert found.answers[0].checked and not found.features
    assert found.answers[0].reason is None


def test_a_jurisdiction_nobody_covers_falls_to_tier_four(registry_factory: Any) -> None:
    """Scope 7.10: "Missing adapter -> generic tier-4 search-and-extract". With
    `NullExtractor` the answer is the same honest `checked=False` it would have been, but
    it arrives through the tier-4 path - so wiring a real extractor in M5 changes one
    constructor argument and not the shape of a coverage entry."""
    found = registry_factory().fetch("closures", [WYANDOTTE], None, DAY)
    answer = found.answers[0]
    assert not answer.checked and answer.tier is None
    assert "tier-4" in (answer.reason or "")


def test_the_null_extractor_never_claims_tier_four_answered() -> None:
    """Reporting tier 4 for a jurisdiction no model looked at would be the subtlest
    available lie: tier 4 means something read a page."""
    result = NullExtractor().extract(
        ExtractionRequest(jurisdiction=MISSOURI, kind="closures", polygon=None, day=DAY),
        AdapterContext(cache=SqliteCache(), budget=Budget()),
    )
    assert not result.answered and result.features == []


# --- ceilings ---------------------------------------------------------------


def test_the_fetch_ceiling_reports_rather_than_truncating(registry_factory: Any) -> None:
    adapters = [
        FakeAdapter(name=f"a{i}", jurisdictions=(f"tiger:county:{29000 + i}",))
        for i in range(MAX_ADAPTER_FETCHES + 3)
    ]
    js = [
        Jurisdiction(id=f"tiger:county:{29000 + i}", level="county", name=f"C{i}")
        for i in range(MAX_ADAPTER_FETCHES + 3)
    ]
    found = registry_factory(*adapters).fetch("closures", js, None, DAY)
    stopped = [a for a in found.answers if a.reason and "ceiling" in a.reason]
    assert stopped, "the ceiling has to be visible, not silent"
    assert sum(1 for a in found.answers if a.checked) == MAX_ADAPTER_FETCHES


def test_too_many_jurisdictions_are_reported_not_dropped(registry_factory: Any) -> None:
    js = [
        Jurisdiction(id=f"tiger:place:{2900000 + i}", level="place", name=f"P{i}")
        for i in range(MAX_JURISDICTIONS + 5)
    ]
    found = registry_factory().fetch("closures", js, None, DAY)
    assert len(found.answers) == len(js), "every jurisdiction gets an answer, even a refusal"
    overflow = [a for a in found.answers if a.reason and "more than" in a.reason]
    assert len(overflow) == 5


# --- discovery --------------------------------------------------------------


def test_discovery_of_an_empty_group_is_empty_rather_than_an_error() -> None:
    found, failures = discover("longrun.adapters.definitely.not.a.real.group")
    assert found == [] and failures == []


def test_the_real_group_discovers_without_raising() -> None:
    """Whatever is registered, `longrun repair` must not die importing it."""
    found, failures = discover()
    assert all(f.reason for f in failures)
    assert all(isinstance(a, Adapter) for a in found)


def test_an_adapter_declaring_a_malformed_id_is_rejected_with_a_reason() -> None:
    """The bug with no symptom: a typo in an id loads fine, matches nothing, and every
    jurisdiction reports "no adapter" exactly as it did before the adapter existed."""
    from longrun.adapters.registry import _rejected

    assert _rejected(FakeAdapter(name="fake.x", jurisdictions=("state:29",))) is not None
    assert (
        _rejected(FakeAdapter(name="fake.x", jurisdictions=("tiger:state:TWENTYNINE",))) is not None
    )
    assert _rejected(FakeAdapter(name="fake.x", jurisdictions=())) is not None
    assert _rejected(FakeAdapter(name="fake.x", jurisdictions=("tiger:state:29",))) is None


def test_an_adapter_is_rejected_for_a_name_kind_or_tier_it_cannot_have() -> None:
    """The name is the cache scope and the registry's grouping key, so a catalog that
    mints one from data must mint a well-formed one."""
    from longrun.adapters.registry import _rejected

    ok = ("tiger:state:29",)
    assert "name" in (_rejected(FakeAdapter(name="Not Dotted", jurisdictions=ok)) or "")
    assert "kind" in (_rejected(FakeAdapter(name="fake.x", jurisdictions=ok, kind="x")) or "")
    assert "tier" in (_rejected(FakeAdapter(name="fake.x", jurisdictions=ok, tier=5)) or "")


def test_one_entry_point_may_register_a_list_and_one_bad_row_fails_alone() -> None:
    """The catalog shape (M21): fifty datasets are one module and one entry point, and a
    broken row is one named failure rather than a catalog that vanishes."""
    from longrun.adapters.base import LoadFailure
    from longrun.adapters.registry import expand

    good = FakeAdapter(name="portal.a", jurisdictions=("tiger:state:29",))
    bad = FakeAdapter(name="portal.b", jurisdictions=("nope",))
    reported = LoadFailure(name="catalog[c]", reason="row c has no dataset")
    found, failures = expand("catalog", [good, bad, reported])
    assert found == [good]
    assert [f.name for f in failures] == ["catalog[portal.b]", "catalog[c]"]

    assert expand("one", good) == ([good], [])
    assert expand("empty", [])[1][0].reason.startswith("declares an empty")
    assert expand("text", "wzdx.modot")[0] == []


def test_a_second_adapter_with_a_taken_name_is_a_failure_not_a_silent_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before M17 `_plan` keyed by name, so the second of two same-named adapters was never
    fetched while its jurisdictions were reported covered. Entry points are walked in name
    order, so the first by *entry-point* name wins regardless of install order."""
    import importlib.metadata

    first = FakeAdapter(name="wzdx.same", jurisdictions=("tiger:state:29",))
    second = FakeAdapter(name="wzdx.same", jurisdictions=("tiger:state:20",))

    class Point:
        def __init__(self, name: str, target: Any) -> None:
            self.name, self._target = name, target

        def load(self) -> Any:
            return self._target

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda group: [Point("b-point", second), Point("a-point", first)],
    )
    found, failures = discover()
    assert found == [first]
    assert (
        len(failures) == 1 and "already registered by entry point 'a-point'" in failures[0].reason
    )


def test_something_that_is_not_an_adapter_is_rejected_with_a_reason() -> None:
    from longrun.adapters.registry import _rejected

    problem = _rejected(object())
    assert problem is not None and "protocol" in problem


@pytest.mark.parametrize(
    "value",
    ["tiger:state:29", "tiger:county:29095", "tiger:place:2938000", "padus:NPS", "padus:CITY:06"],
)
def test_the_grammar_accepts_every_id_jurisdiction_mints(value: str) -> None:
    assert valid_jurisdiction_id(value)


@pytest.mark.parametrize(
    "value", ["", "tiger:state", "tiger:borough:29", "padus:", "nps", "tiger:state:2938000000"]
)
def test_the_grammar_rejects_what_it_should(value: str) -> None:
    assert not valid_jurisdiction_id(value)


def test_every_declared_entry_point_target_imports() -> None:
    """Reads `pyproject.toml` rather than installed metadata, so a typo in the table is
    caught in the same commit that introduces it.

    `importlib.metadata` sees only what was installed, so `discover()` cannot notice a
    freshly-edited table until somebody re-syncs - and a broken entry there is invisible by
    construction: the adapter never loads, and every jurisdiction reports "no adapter"
    exactly as it did before.
    """
    import importlib
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = config["project"].get("entry-points", {}).get("longrun.adapters", {})

    from longrun.adapters.registry import expand

    names: list[str] = []
    for name, target in declared.items():
        module_path, _, attribute = target.partition(":")
        module = importlib.import_module(module_path)
        assert attribute, f"{name} declares no attribute: {target}"
        loaded = getattr(module, attribute, None)
        assert loaded is not None, f"{name} points at {target}, which does not exist"
        # The same judgement `discover()` makes, so the two cannot disagree about a target.
        adapters, failures = expand(name, loaded)
        assert not failures, [f"{f.name}: {f.reason}" for f in failures]
        names.extend(a.name for a in adapters)
    assert len(names) == len(set(names)), "two entry points register the same adapter name"


# --- the ladder (M17, ADR 0047) ---------------------------------------------


class _Key:
    """An `ApiKey`-shaped credential whose value the test decides."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def value(self) -> str | None:
        return self._value


def test_a_failed_tier_falls_through_to_the_next(registry_factory: Any) -> None:
    """Until M17 only the best covering tier was asked, so a tier-1 feed that was down left
    Missouri unchecked while a working tier-3 portal sat unasked."""
    down = FakeAdapter(name="t.one", jurisdictions=("tiger:state:29",), raises=RuntimeError("503"))
    portal = FakeAdapter(
        name="t.three",
        jurisdictions=("tiger:state:29",),
        tier=3,
        result=AdapterResult(features=[_feature(tier=3, confidence=0.7)]),
    )
    found = registry_factory(down, portal).fetch("closures", [MISSOURI], None, DAY)
    answer = found.answers[0]
    assert answer.checked and answer.tier == 3 and answer.adapter == "t.three"
    assert answer.reason == "tier 1 t.one: RuntimeError: 503", "the failure is named, long-form"
    assert [a.outcome for a in answer.attempts] == ["failed", "answered"]
    assert len(found.features) == 1


def test_an_empty_answer_stops_the_ladder(registry_factory: Any) -> None:
    """A feed read with nothing in it is evidence, and the next tier is not asked."""
    quiet = FakeAdapter(name="t.one", jurisdictions=("tiger:state:29",))
    portal = FakeAdapter(name="t.three", jurisdictions=("tiger:state:29",), tier=3)
    found = registry_factory(quiet, portal).fetch("closures", [MISSOURI], None, DAY)
    assert found.answers[0].checked and found.answers[0].tier == 1
    assert portal.calls == []


def test_a_state_feed_does_not_pre_empt_a_city_portal(registry_factory: Any) -> None:
    """Per claimant: Missouri's feed climbs Missouri's ladder and the city's portal climbs
    the city's. With one ladder per jurisdiction the tier-1 state feed would answer for
    Kansas City and its permit portal would never be asked."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    permits = FakeAdapter(name="portal.kc", jurisdictions=("tiger:place:2938000",), tier=3)
    found = registry_factory(modot, permits).fetch("closures", [KCMO], None, DAY)
    assert len(modot.calls) == 1 and len(permits.calls) == 1
    answer = found.answers[0]
    assert answer.checked and answer.tier == 1
    assert answer.adapter == "wzdx.modot; portal.kc"


def test_an_incomplete_source_contributes_without_ending_its_ladder(
    registry_factory: Any,
) -> None:
    """The Pennsylvania Turnpike claims Pennsylvania and publishes only its own road, so its
    answer adds records and the statewide tier below it is still asked."""
    pa = Jurisdiction(id="tiger:state:42", level="state", name="Pennsylvania")
    turnpike = FakeAdapter(
        name="wzdx.paturnpike",
        jurisdictions=("tiger:state:42",),
        result=AdapterResult(features=[_feature(detail="turnpike", category="turnpike")]),
    )
    turnpike.complete = False  # type: ignore[attr-defined]
    statewide = FakeAdapter(
        name="state511.pa",
        jurisdictions=("tiger:state:42",),
        tier=2,
        result=AdapterResult(features=[_feature(tier=2)]),
    )
    found = registry_factory(turnpike, statewide).fetch("closures", [pa], None, DAY)
    assert len(statewide.calls) == 1
    assert len(found.features) == 2 and found.answers[0].tier == 1


def test_complementary_facets_are_all_asked(registry_factory: Any) -> None:
    """Work zones and street-use permits describe different things; a facet is how a
    source says it is a complement rather than a substitute."""
    zones = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    permits = FakeAdapter(name="portal.mo", jurisdictions=("tiger:state:29",), tier=3)
    permits.facet = "permits"  # type: ignore[attr-defined]
    registry_factory(zones, permits).fetch("closures", [MISSOURI], None, DAY)
    assert len(zones.calls) == 1 and len(permits.calls) == 1


def test_a_missing_key_is_skipped_for_free_and_falls_through(registry_factory: Any) -> None:
    """A keyed adapter is still asked so it can replay what was recorded; with nothing to
    replay it reports `key_missing`, spends no slot, and the next tier is tried."""
    keyed = FakeAdapter(
        name="wzdx.keyed",
        jurisdictions=("tiger:state:29",),
        result=AdapterResult(reason="LONGRUN_X is not set", key_missing=True),
    )
    keyed.key = _Key(None)  # type: ignore[attr-defined]
    portal = FakeAdapter(name="portal.mo", jurisdictions=("tiger:state:29",), tier=3)
    registry = registry_factory(keyed, portal)
    found = registry.fetch("closures", [MISSOURI], None, DAY)
    assert found.answers[0].checked and found.answers[0].tier == 3
    assert found.answers[0].attempts[0].outcome == "skipped"
    assert registry._fetches == {"closures": 1}, "only the portal's fetch is counted"


def test_a_ceiling_ends_a_ladder_and_says_so_in_the_manifest() -> None:
    """A ceiling is a statement about this plan's budget, not the jurisdiction's sources,
    so it never falls through to tier 4 - and it reaches the manifest as a degradation."""
    budget = Budget()
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    registry = AdapterRegistry(SqliteCache(), budget, adapters=[modot])
    registry._fetches["closures"] = MAX_ADAPTER_FETCHES
    found = registry.fetch("closures", [MISSOURI], None, DAY)
    assert "ceiling" in (found.answers[0].reason or "")
    assert "tier 4" not in (found.answers[0].reason or "")
    assert modot.calls == []
    assert any("ceiling" in line for line in budget.degradation)


def test_the_same_question_twice_is_one_fetch(registry_factory: Any) -> None:
    """The loop scores every candidate on one context. Before M17 each pass spent a fresh
    slot on the same answer, and candidates scored later met the ceiling first."""
    from shapely.geometry import box

    feed = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    feed.scope = "feed"  # type: ignore[attr-defined]
    portal = FakeAdapter(name="portal.mo", jurisdictions=("tiger:state:29",))
    portal.facet = "permits"  # type: ignore[attr-defined]
    registry = registry_factory(feed, portal)
    for corridor in (box(0, 0, 1, 1), box(0, 0, 1, 1), box(5, 5, 6, 6)):
        registry.fetch("closures", [MISSOURI], corridor, DAY)
    assert len(feed.calls) == 1, "a statewide feed does not depend on the corridor"
    assert len(portal.calls) == 2, "a bounding-box source does, and is asked per corridor"
    assert registry._fetches == {"closures": 3}


def test_one_kind_exhausting_its_ceiling_leaves_another_alone(registry_factory: Any) -> None:
    registry = registry_factory(
        FakeAdapter(name="portal.nps", jurisdictions=("tiger:state:29",), kind="trail_status")
    )
    registry._fetches["closures"] = MAX_ADAPTER_FETCHES
    found = registry.fetch("trail_status", [MISSOURI], None, DAY)
    assert found.answers[0].checked


def test_the_null_extractor_spends_nothing(registry_factory: Any) -> None:
    """It reads no page, so charging it a slot was charging for a sentence."""
    js = [
        Jurisdiction(id=f"tiger:county:{29000 + i}", level="county", name=f"C{i}")
        for i in range(MAX_ADAPTER_FETCHES + 5)
    ]
    registry = registry_factory()
    found = registry.fetch("closures", js, None, DAY)
    assert not any("ceiling" in (a.reason or "") for a in found.answers)
    assert registry._fetches == {}


def test_an_extractor_that_raises_is_a_reason_not_a_crash(registry_factory: Any) -> None:
    """Before M17 `_extracted` had no try/except, so one bad page date took down the whole
    scorer - tier-1 answers included."""

    class Exploding:
        def extract(self, request: Any, ctx: Any) -> AdapterResult:
            raise ValueError("feature end precedes its start")

    found = registry_factory(extractor=Exploding()).fetch("closures", [WYANDOTTE], None, DAY)
    assert not found.answers[0].checked
    assert "feature end precedes its start" in (found.answers[0].reason or "")


def test_a_failed_peer_is_named_on_an_answered_jurisdiction(registry_factory: Any) -> None:
    """Phoenix: AZDOT answered and Maricopa's cassette was missing. The jurisdiction is
    checked, and the sheet still says which of its two sources did not answer."""
    phoenix = Jurisdiction(
        id="tiger:place:0455000",
        level="place",
        name="Phoenix",
        within=("tiger:state:04", "tiger:county:04013"),
    )
    azdot = FakeAdapter(name="wzdx.azdot", jurisdictions=("tiger:state:04",))
    maricopa = FakeAdapter(
        name="wzdx.maricopa",
        jurisdictions=("tiger:county:04013",),
        result=AdapterResult(reason="not in the cassette"),
    )
    found = registry_factory(azdot, maricopa).fetch("closures", [phoenix], None, DAY)
    answer = found.answers[0]
    assert answer.checked and answer.tier == 1
    assert answer.reason == "tier 1 wzdx.maricopa: not in the cassette"


# --- volume hygiene (M17) ---------------------------------------------------


def _at(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def test_a_statewide_answer_is_clipped_to_the_corridor(registry_factory: Any) -> None:
    """Jackson County used to "have" all of Missouri's work zones: the feed is statewide and
    every jurisdiction it covered reported the state's total. A record with no geometry is
    about its whole jurisdiction and is kept."""
    from shapely.geometry import box

    feed = FakeAdapter(
        name="wzdx.modot",
        jurisdictions=("tiger:state:29",),
        result=AdapterResult(
            features=[
                _feature(geometry=_at(-94.5, 39.1)),  # Kansas City, in the corridor
                _feature(geometry=_at(-90.2, 38.6)),  # St Louis, not
                _feature(geometry={}, category="statewide notice"),
            ]
        ),
    )
    found = registry_factory(feed).fetch("closures", [JACKSON], box(-94.7, 38.9, -94.3, 39.3), DAY)
    assert len(found.features) == 2
    assert found.answers[0].count == 2
    assert all(f.geometry.get("coordinates") != [-90.2, 38.6] for f in found.features)


def test_one_record_published_by_two_peers_is_counted_once(registry_factory: Any) -> None:
    """An aggregator republishing a state feed is two sources and one work zone."""
    zone = _feature(geometry=_at(-112.07, 33.45), category="road-closure")
    azdot = FakeAdapter(
        name="wzdx.azdot", jurisdictions=("tiger:state:04",), result=AdapterResult([zone])
    )
    maricopa = FakeAdapter(
        name="wzdx.maricopa",
        jurisdictions=("tiger:county:04013",),
        result=AdapterResult([zone.model_copy(update={"ref": "their-own-id"})]),
    )
    phoenix = Jurisdiction(
        id="tiger:place:0455000",
        level="place",
        name="Phoenix",
        within=("tiger:state:04", "tiger:county:04013"),
    )
    found = registry_factory(azdot, maricopa).fetch("closures", [phoenix], None, DAY)
    assert len(found.features) == 1
    assert found.answers[0].count == 1


def test_who_covered_a_jurisdiction_reaches_the_coverage_entry(registry_factory: Any) -> None:
    """`covered_by` stopped at `JurisdictionAnswer`, so the sheet could not say one fetch
    had covered a dozen places."""
    modot = FakeAdapter(name="wzdx.modot", jurisdictions=("tiger:state:29",))
    found = registry_factory(modot).fetch("closures", [KCMO], None, DAY)
    assert found.answers[0].coverage("closures").covered_by == "tiger:state:29"
