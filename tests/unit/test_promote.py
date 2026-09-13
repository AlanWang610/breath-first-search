"""Drafting an adapter from a tier-4 extraction (scope 7.10).

`draft_adapter` is a pure function returning source text, which is what makes this testable
with a hand-built record, no model and no filesystem. The split matters more than usual
because the thing under test is **generated code**, and the inputs come from a model reading
a web page - the least trustworthy data in the project.

So the assertions worth making are: does it parse, does it declare what it claims, and does
it survive a park name with a triple quote in it.
"""

from __future__ import annotations

import ast

import pytest

from longrun.adapters.promote import DRAFT_TIER, draft_adapter, drafts_parse
from longrun.core.models.features import MAX_EXTRACTION_CONFIDENCE
from longrun.core.models.jurisdiction import Jurisdiction

BACKSLASH = chr(92)

KCMO = Jurisdiction(
    id="tiger:place:2938000",
    level="place",
    name="Kansas City",
    within=("tiger:state:29",),
)


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _class(source: str) -> ast.ClassDef:
    return next(n for n in _tree(source).body if isinstance(n, ast.ClassDef))


def _attr(source: str, name: str) -> object:
    for node in _class(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not declared")


# --- it parses --------------------------------------------------------------


def test_a_draft_is_valid_python() -> None:
    assert drafts_parse(draft_adapter(KCMO, "closures", "https://www.kcmo.gov/closures"))


@pytest.mark.parametrize(
    "name",
    [
        'Park """ breaks the docstring',
        'Ende"s Park',
        "O'Fallon Park",
        "tab\there",
        "nul\x00here",
        "line\nbreak",
        "trailing" + BACKSLASH,
        "",
    ],
    ids=["triple-quote", "quote", "apostrophe", "tab", "nul", "newline", "backslash", "empty"],
)
def test_a_hostile_jurisdiction_name_still_drafts(name: str) -> None:
    """A tier-4 extraction gets its names from a model reading a web page. A triple quote
    ends the generated docstring early and produces a module nobody can open, which is the
    one failure that makes a draft worthless rather than merely wrong."""
    jurisdiction = Jurisdiction(id="padus:CITY:29", level="park", name=name)
    assert drafts_parse(draft_adapter(jurisdiction, "trail_status", "https://example.gov"))


@pytest.mark.parametrize(
    "url",
    ['https://x.gov/"""', "https://x.gov/a" + BACKSLASH, "https://x.gov/\x00", ""],
    ids=["triple-quote", "backslash", "nul", "empty"],
)
def test_a_hostile_url_still_drafts(url: str) -> None:
    assert drafts_parse(draft_adapter(KCMO, "closures", url))


def test_hostile_notes_still_draft() -> None:
    draft = draft_adapter(KCMO, "closures", "https://x.gov", notes='found """ here')
    assert drafts_parse(draft)


# --- it declares what it claims ---------------------------------------------


def test_a_draft_declares_the_jurisdiction_it_was_promoted_for() -> None:
    source = draft_adapter(KCMO, "closures", "https://www.kcmo.gov/closures").source
    assert _attr(source, "jurisdictions") == ("tiger:place:2938000",)
    assert _attr(source, "kind") == "closures"


def test_a_draft_declares_a_jurisdiction_id_the_registry_would_accept() -> None:
    """A malformed id is the adapter bug with no symptom - it loads, matches nothing, and
    every jurisdiction reports "no adapter" exactly as before."""
    from longrun.adapters.base import valid_jurisdiction_id

    source = draft_adapter(KCMO, "closures", "https://x.gov").source
    ids = _attr(source, "jurisdictions")
    assert isinstance(ids, tuple)
    assert all(valid_jurisdiction_id(i) for i in ids)


def test_a_draft_is_tier_four_and_cannot_be_promoted_by_drafting() -> None:
    """The rule the whole module rests on. A model's guess about a URL must not become a
    source of record, and `MIN_HARD_FLAG_TIER` would be guarding nothing if a draft could
    claim the tier it wanted."""
    source = draft_adapter(KCMO, "closures", "https://x.gov").source
    assert _attr(source, "tier") == DRAFT_TIER == 4


def test_a_drafted_tier_provably_cannot_hard_fail_a_route() -> None:
    """By arithmetic: `MAX_EXTRACTION_CONFIDENCE` is 0.5 and `HARD_FLAG_CONFIDENCE` 0.8, so
    nothing a draft returns can trip check 6 even if somebody registers it unreviewed."""
    from longrun.core.scorers.closures import HARD_FLAG_CONFIDENCE, MIN_HARD_FLAG_TIER

    assert DRAFT_TIER > MIN_HARD_FLAG_TIER
    assert MAX_EXTRACTION_CONFIDENCE < HARD_FLAG_CONFIDENCE


def test_the_fetch_body_refuses_rather_than_guessing_a_parser() -> None:
    """An extraction knows *where* a jurisdiction publishes and nothing about the shape of
    what is there. A plausible-looking wrong parser is worse than an obvious blank."""
    source = draft_adapter(KCMO, "closures", "https://x.gov").source
    fetch = next(
        n for n in _class(source).body if isinstance(n, ast.FunctionDef) and n.name == "fetch"
    )
    raises = [n for n in ast.walk(fetch) if isinstance(n, ast.Raise)]
    assert raises, "a drafted fetch must not silently return an empty result"


def test_a_draft_says_out_loud_that_it_is_unreviewed() -> None:
    source = draft_adapter(KCMO, "closures", "https://x.gov").source
    docstring = ast.get_docstring(_tree(source)) or ""
    assert "not registered" in docstring.lower()
    assert "does not work yet" in docstring.lower()


# --- registration is a human act --------------------------------------------


def test_the_registration_line_is_returned_and_never_written() -> None:
    """Automatic registration would let a model's guess become a tier-1 source of record,
    which inverts the whole point of tiers. Somebody has to paste this."""
    draft = draft_adapter(KCMO, "closures", "https://x.gov")
    assert draft.registration.endswith('"')
    assert draft.module_name in draft.registration
    assert "longrun.adapters.jurisdictions" in draft.module_name


def test_the_module_name_is_importable_as_written() -> None:
    """`padus:CITY:29` and "Kansas City" are not identifiers, and a drafted module whose
    name does not import is a draft nobody can review."""
    for jurisdiction in (
        KCMO,
        Jurisdiction(id="padus:CITY:29", level="park", name="Kansas City"),
        Jurisdiction(id="padus:NPS", level="park", name="NPS"),
    ):
        draft = draft_adapter(jurisdiction, "closures", "https://x.gov")
        tail = draft.module_name.rsplit(".", 1)[-1]
        assert tail.isidentifier(), tail


def test_two_kinds_for_one_jurisdiction_do_not_collide() -> None:
    """One module may export several kinds, and the entry-point names have to differ."""
    a = draft_adapter(KCMO, "closures", "https://x.gov")
    b = draft_adapter(KCMO, "trail_status", "https://x.gov")
    assert a.entry_point != b.entry_point


# --- region step 4 ----------------------------------------------------------


def test_region_step_four_reports_real_adapter_coverage() -> None:
    """The half of scope 13 step 4 that has said "the registry is M4" since M3."""
    from longrun.regions.build import adapter_coverage

    covered, by_tier = adapter_coverage(
        [
            Jurisdiction(id="tiger:state:29", level="state", name="Missouri"),
            Jurisdiction(id="tiger:state:20", level="state", name="Kansas"),
            Jurisdiction(id="tiger:county:06075", level="county", name="San Francisco County"),
            Jurisdiction(id="tiger:state:56", level="state", name="Wyoming"),
        ]
    )
    names = {j.name for j in covered}
    assert {"Missouri", "Kansas"} <= names, "the two keyless DOT feeds have to be found"
    assert "Wyoming" not in names, "a state with no adapter must not be reported as covered"
    assert by_tier.get(1, 0) >= 2


def test_adapter_lookup_spends_no_budget() -> None:
    """A region build has no corridor, no date and no `ScorerContext` to spend a budget
    from, so step 4 must be able to ask who exists without asking what they say."""
    from longrun.adapters.registry import AdapterRegistry
    from longrun.core.data.cache import SqliteCache
    from longrun.core.models.context import Budget

    with SqliteCache() as cache:
        budget = Budget()
        registry = AdapterRegistry(cache, budget, offline=True)
        registry.adapters_for(
            "closures", Jurisdiction(id="tiger:state:29", level="state", name="Missouri")
        )
    assert budget.api_calls_used == 0
