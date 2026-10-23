"""Tier 4, which had five bugs and no tests (scope 7.10; M13.5).

`ModelExtractor` had **zero instantiations and zero tests** in the whole tree: the registry
builds a `NullExtractor` unless a caller passes one, and no caller does. So every defect on
the path from "a jurisdiction nobody covers" to "a record on the plan sheet" was latent, and
each one below would have appeared as bad output rather than as an error the first time an
extractor was wired in.

M13.5 named four. Writing the first of them a test found a fifth, and it is the one that
mattered most: `fetch` collected features from the `AdapterResult`s in its adapter loop and
never from the extraction, so a tier-4 record reached nothing at all. The reported bug -
`polygon=None`, so `runs_along` returns `inf` and `closures` drops the record - is real and
is fixed, but it was never the reason the records went missing. Both had to be fixed for
either to matter, which is why they share a section here.

The model itself is never called here. `ModelExtractor.extract` resolves `build_agent` and
`ask` from `longrun.agent.model` at call time, so a fake answer is substituted there - which
is also what `conftest._block_model` insists on: a unit test that reached a model would pass
on a developer's machine and fail in CI (ADR 0015).

The registry side uses a fake `Extractor` for the same reason the adapter tests use fake
adapters: what is being tested is the arithmetic of who gets asked and what the answer says,
not anybody's real page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from shapely.geometry import Polygon

from longrun.adapters.base import AdapterContext, AdapterResult
from longrun.adapters.extraction.model import (
    MAX_CONFIDENCE,
    ExtractedClosure,
    Extraction,
    ModelExtractor,
)
from longrun.adapters.extraction.seam import ExtractionRequest
from longrun.adapters.promote import draft_adapter, drafts_parse
from longrun.adapters.registry import MAX_ADAPTER_FETCHES, AdapterRegistry
from longrun.core.data.cache import SqliteCache
from longrun.core.models.context import Budget
from longrun.core.models.features import Feature
from longrun.core.models.jurisdiction import Jurisdiction

DAY = date(2026, 9, 26)

#: A jurisdiction no adapter in these tests covers, so every route through here reaches the
#: tier-4 seam rather than an adapter.
UNCOVERED = Jurisdiction(id="padus:OTHF", level="park", name="Ozark National Scenic Riverway")

#: Stands in for the corridor `closures` passes to `FeatureSource.fetch`. A real polygon,
#: because half of what is under test is that a tier-4 record ends up *somewhere* rather
#: than in an empty `GeometryCollection`.
CORRIDOR = Polygon([(-91.45, 37.14), (-91.35, 37.14), (-91.35, 37.16), (-91.45, 37.16)])


@dataclass
class FakeExtractor:
    """An `Extractor` that answers from a script and records what it was asked."""

    result: AdapterResult = field(default_factory=AdapterResult)
    seen: list[ExtractionRequest] = field(default_factory=list)

    def extract(self, request: ExtractionRequest, ctx: AdapterContext) -> AdapterResult:
        self.seen.append(request)
        return self.result


def _extracted_feature(confidence: float, what: str | None = None) -> Feature:
    return Feature(
        kind="closures",
        category="extracted",
        geometry={"type": "Point", "coordinates": [-91.4, 37.15]},
        tier=4,
        confidence=confidence,
        jurisdiction=UNCOVERED.id,
        detail=what,
    )


def _registry(extractor: Any, *adapters: Any) -> AdapterRegistry:
    return AdapterRegistry(SqliteCache(), Budget(), adapters=list(adapters), extractor=extractor)


def _answer_from(extractor: FakeExtractor) -> Any:
    found = _registry(extractor).fetch("closures", [UNCOVERED], CORRIDOR, DAY)
    return found.answers[0]


# --- bug 1: the question was asked about nothing ----------------------------


def test_the_extraction_is_asked_about_the_corridor_rather_than_about_nothing() -> None:
    """`polygon=None` made every tier-4 record a feature with no extent.

    `model._geometry` falls back to the shape it is handed, `None` produces an empty
    `GeometryCollection`, `runs_along` returns `inf`, and `closures` drops the record for
    being off-route - while `JurisdictionAnswer.count` still counts it. The sheet then says
    "tier 4 answered, 3 records" and shows no flag, which is the worst shape a bug can have:
    a number that is right and a consequence that never happens.
    """
    extractor = FakeExtractor()
    _registry(extractor).fetch("closures", [UNCOVERED], CORRIDOR, DAY)

    assert extractor.seen, "the uncovered jurisdiction never reached the tier-4 seam"
    assert extractor.seen[0].polygon is CORRIDOR


def test_a_tier_four_record_lands_on_the_polygon_it_was_asked_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same bug, at the end that produces the geometry.

    Run through the **registry** with a real `ModelExtractor` rather than by calling
    `_geometry` with a polygon this test chose. A test of the helper alone passes happily
    throughout the period the registry is handing it `None`, which is the whole period the
    bug existed - it is the join that was broken, so it is the join that gets asserted.
    """
    extractor = _scripted_extractor(
        monkeypatch, Extraction(closures=[ExtractedClosure(what="bridge out")])
    )

    found = _registry(extractor).fetch("closures", [UNCOVERED], CORRIDOR, DAY)

    assert found.features, "the extraction produced no record to check"
    geometry = found.features[0].geometry
    assert geometry["type"] == "Polygon"
    assert geometry.get("coordinates"), "an empty GeometryCollection is the bug, not the fix"


def test_an_extracted_record_reaches_the_feature_set_and_not_only_the_count() -> None:
    """The mechanism M13.5's first bug actually had, which is not the one it was reported as.

    `fetch` collects features from the `AdapterResult`s in its `plan` loop. A tier-4
    extraction has no entry in `plan` and no `AdapterResult` in `results` - it happens inside
    `_answer` - so its records went nowhere while `JurisdictionAnswer.count` still counted
    them. "Tier 4 answered, N records, zero flags" was therefore reachable *without* any
    geometry being wrong, and threading the polygon through would not have fixed it on its
    own.

    `count` and the set are asserted together because the two disagreeing is the whole bug.
    """
    extractor = FakeExtractor(
        result=AdapterResult(
            features=[
                _extracted_feature(0.3, "Low-water crossing closed"),
                _extracted_feature(0.3, "Campground road washed out"),
            ]
        )
    )

    found = _registry(extractor).fetch("closures", [UNCOVERED], CORRIDOR, DAY)

    assert found.answers[0].count == 2
    assert len(found.features) == 2
    assert all(f.tier == 4 and f.jurisdiction == UNCOVERED.id for f in found.features)


def test_an_extraction_that_failed_contributes_no_records() -> None:
    """The negative control for the test above.

    `AdapterResult` with a reason is "nobody read a page", and a reason arriving alongside
    features would be the one case where collecting them unconditionally would put
    unanswered records into a plan. `answered` gates it, and this is what says so.
    """
    extractor = FakeExtractor(
        result=AdapterResult(features=[_extracted_feature(0.3)], reason="no page to read")
    )

    found = _registry(extractor).fetch("closures", [UNCOVERED], CORRIDOR, DAY)

    assert not found.answers[0].checked
    assert found.features == []


# --- bug 2: tier 4 ran outside the budget it was said to live behind --------


def test_tier_four_extraction_spends_the_adapter_fetch_ceiling() -> None:
    """`Budget.model_calls_max`'s own comment says tier 4 is "capped separately, by the
    adapter fetch limits it already lives behind". It was not: `_extracted` neither checked
    nor incremented the counter `_ask` maintains.

    The consequence is not a slow plan, it is a quiet one. A route crossing forty uncovered
    jurisdictions makes up to forty model calls against a twelve-call ceiling, and `ask`
    returns `None` on `BudgetExceeded` rather than raising - so calls thirteen onward come
    back as "tier-4 extraction returned nothing", a sentence about the page rather than
    about the budget.
    """
    extractor = FakeExtractor(result=AdapterResult(features=[]))
    crossed = [
        Jurisdiction(id=f"padus:OTHF:{29 + i:02d}", level="park", name=f"Park {i}")
        for i in range(MAX_ADAPTER_FETCHES + 6)
    ]

    found = _registry(extractor).fetch("closures", crossed, CORRIDOR, DAY)

    assert len(extractor.seen) == MAX_ADAPTER_FETCHES
    stopped = [a for a in found.answers if a.reason and "ceiling" in a.reason]
    assert len(stopped) == 6, "the jurisdictions past the ceiling have to say so"
    assert all(not a.checked for a in stopped)


def test_an_adapter_fetch_and_an_extraction_spend_the_same_ceiling() -> None:
    """One counter, because it is one budget.

    Separate ceilings would let a route with a few covered jurisdictions and many uncovered
    ones spend twice the intended total, which is the shape of route this fallback exists
    for.
    """
    from tests.unit.test_adapter_registry import FakeAdapter

    covered = [
        Jurisdiction(id=f"tiger:county:{29000 + i}", level="county", name=f"C{i}")
        for i in range(MAX_ADAPTER_FETCHES - 2)
    ]
    adapters = [FakeAdapter(name=f"a{i}", jurisdictions=(j.id,)) for i, j in enumerate(covered)]
    uncovered = [
        Jurisdiction(id=f"padus:OTHF:{40 + i:02d}", level="park", name=f"P{i}") for i in range(5)
    ]
    extractor = FakeExtractor()

    _registry(extractor, *adapters).fetch("closures", [*covered, *uncovered], CORRIDOR, DAY)

    assert len(extractor.seen) == 2, "the adapters had already spent all but two fetches"


# --- bug 3: promotion had no provenance to promote --------------------------


def test_an_extracted_record_carries_the_url_a_promotion_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.10 ends with promotion to a drafted adapter, and `draft_adapter` takes a
    `source_url` as a required argument. Nothing in the tree produced one: `ExtractedClosure`
    had no URL field, so the promotion path could be tested with a hand-written URL and could
    never be reached from a real extraction.

    The draft still refuses to guess a parser, and still drafts at tier 4. What changed is
    that there is now something to open.
    """
    page_url = "https://www.nps.gov/ozar/planyourvisit/conditions.htm"
    result = _extract_with(
        monkeypatch,
        Extraction(
            closures=[ExtractedClosure(what="Powder Mill access road closed", source_url=page_url)]
        ),
    )

    assert result.features[0].source_url == page_url
    assert result.source_url == page_url

    draft = draft_adapter(UNCOVERED, "closures", result.features[0].source_url or "")
    assert drafts_parse(draft)
    assert page_url in draft.source


def test_a_page_that_names_no_url_promotes_nothing_rather_than_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control, and it is the one that matters.

    A URL is the single thing on a drafted adapter a reviewer cannot check by reading the
    draft, so a model composing a plausible one is the failure this field could introduce.
    `None` stays `None` all the way to the `Feature`.
    """
    result = _extract_with(monkeypatch, Extraction(closures=[ExtractedClosure(what="trail wet")]))

    assert result.features[0].source_url is None
    assert result.source_url is None


# --- bug 4: "unverified" was conveyed by the tier alone ---------------------


def test_a_tier_four_answer_reports_its_confidence_to_the_manifest() -> None:
    """§7.10 asks for "a manifest entry marked unverified", and `JurisdictionAnswer
    .confidence` - which `CoverageEntry` carries and the sheet prints - was left empty on
    every tier-4 answer. The tier said unverified; the number said nothing.

    The **mean** rather than the maximum: one confident line must not speak for a page of
    vague ones, and mean is how every other confidence in this project is summarised.
    """
    extractor = FakeExtractor(
        result=AdapterResult(
            features=[
                _extracted_feature(0.2, "Trail muddy past mile 4"),
                _extracted_feature(0.4, "Bridge out at Big Spring"),
            ]
        )
    )

    answer = _answer_from(extractor)

    assert answer.checked and answer.tier == 4 and answer.count == 2
    assert answer.confidence == pytest.approx(0.3)
    assert answer.coverage("closures").confidence == pytest.approx(0.3)


def test_an_extraction_that_found_nothing_invents_no_confidence() -> None:
    """ "The page states no closure" is a checked answer with nothing to attach a number to.

    A confidence here would be a number about nothing, and the tier already says the reading
    is unverified. This is the assertion that stops the fix above from becoming an invented
    default.
    """
    answer = _answer_from(FakeExtractor(result=AdapterResult(features=[])))

    assert answer.checked and answer.tier == 4
    assert answer.confidence is None


def test_the_unwired_seam_still_reports_no_tier_and_no_confidence() -> None:
    """The default path, unchanged - and asserted, because every golden route depends on it.

    `AdapterRegistry` builds a `NullExtractor` when no extractor is injected, and six
    `expected.json` files pin the coverage entry it produces. A confidence appearing here
    would move all of them.
    """
    found = AdapterRegistry(SqliteCache(), Budget(), adapters=[]).fetch(
        "closures", [UNCOVERED], CORRIDOR, DAY
    )
    answer = found.answers[0]

    assert not answer.checked
    assert answer.tier is None and answer.confidence is None
    assert "tier-4" in (answer.reason or "")


# --- the bound the whole tier rests on --------------------------------------


def test_an_over_confident_extraction_degrades_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Feature`'s validator rejects tier 4 above 0.5, so capping at the producer is what
    turns an over-confident model into a weaker record instead of a failed plan - the same
    treatment every other unreliable source here gets."""
    result = _extract_with(
        monkeypatch,
        Extraction(closures=[ExtractedClosure(what="road closed", confidence=MAX_CONFIDENCE)]),
    )

    assert result.features[0].confidence == MAX_CONFIDENCE
    assert result.features[0].tier == 4


def _scripted_extractor(monkeypatch: pytest.MonkeyPatch, answer: Extraction) -> ModelExtractor:
    """The real `ModelExtractor` over a scripted answer, with no model anywhere.

    `extract` imports `build_agent` and `ask` from `longrun.agent.model` at call time, so
    replacing them on that module is enough and no pydantic-ai agent is ever constructed -
    which `conftest._block_model` requires and ADR 0015 is the reason for.
    """
    from longrun.agent import model as agent_model

    monkeypatch.setattr(agent_model, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(agent_model, "ask", lambda *a, **k: answer)
    return ModelExtractor(pages=lambda request: "a page of text")


def _extract_with(monkeypatch: pytest.MonkeyPatch, answer: Extraction) -> AdapterResult:
    """One scripted extraction, called directly, for the assertions about a record."""
    return _scripted_extractor(monkeypatch, answer).extract(
        ExtractionRequest(jurisdiction=UNCOVERED, kind="closures", polygon=CORRIDOR, day=DAY),
        AdapterContext(cache=SqliteCache(), budget=Budget()),
    )
