"""The adapter seam, before any adapter exists (scope 7.10, 3.6).

`NullFeatureSource` is not a placeholder to be deleted when the registry lands. It is the
answer a plan gives when nobody wired a registry up, and M1 promised exactly this shape and
never delivered it: the three access scorers were supposed to ship "with zero registered
adapters, so each returns empty with a coverage entry". Instead `cli/repair.py` fabricated
the entries in a loop from a dict of prose. This is what makes the honest answer a property
of the seam rather than of a hard-coded string.
"""

from __future__ import annotations

from datetime import date

from longrun.core.data.base import FeatureSource, NullFeatureSource
from longrun.core.models.context import ScorerContext
from longrun.core.models.jurisdiction import Jurisdiction

DAY = date(2026, 9, 15)

MISSOURI = Jurisdiction(id="tiger:state:29", level="state", name="Missouri")
KANSAS = Jurisdiction(id="tiger:state:20", level="state", name="Kansas")


def test_the_null_source_satisfies_the_protocol() -> None:
    """`runtime_checkable` only checks method names, which is exactly the drift worth
    catching: a real registry that renamed `fetch` would still type-check at its own call
    sites and fail here."""
    assert isinstance(NullFeatureSource(), FeatureSource)


def test_no_registry_is_reported_per_jurisdiction_rather_than_once() -> None:
    """A route crossing a state line has two jurisdictions with no adapter, not one
    unavailable scorer. Scope 7.6 wants the answer "for each jurisdiction crossed"."""
    found = NullFeatureSource().fetch("closures", [MISSOURI, KANSAS], None, DAY)
    assert [a.jurisdiction for a in found.answers] == ["tiger:state:29", "tiger:state:20"]
    assert all(not a.checked for a in found.answers)
    assert found.missing_adapters == ["tiger:state:29", "tiger:state:20"]


def test_the_null_source_never_claims_a_tier() -> None:
    """The subtlest way to lie here: reporting tier 4 for a jurisdiction nobody asked.
    Tier 4 means an LLM read a page and produced something; `None` means silence."""
    found = NullFeatureSource().fetch("trail_status", [MISSOURI], None, DAY)
    assert found.answers[0].tier is None


def test_no_registry_is_not_a_clean_sheet() -> None:
    """The failure this whole milestone exists to prevent: an empty result read as "no
    closures on this route"."""
    found = NullFeatureSource().fetch("closures", [MISSOURI], None, DAY)
    assert not found.features
    assert not found.answered, "no features AND nobody asked is not an all-clear"
    assert found.answers[0].reason


def test_asking_about_nowhere_answers_about_nowhere() -> None:
    """A route that crossed no resolvable jurisdiction produces no answers - and therefore
    still reports `answered` false, rather than an empty list reading as success."""
    found = NullFeatureSource().fetch("closures", [], None, DAY)
    assert found.answers == [] and not found.answered


def test_the_null_source_claims_no_adapters() -> None:
    assert NullFeatureSource().adapters_for("closures", MISSOURI) == []


def test_a_context_without_a_registry_says_so_by_being_none() -> None:
    """`None` rather than a `NullFeatureSource` default. "No registry was configured" and
    "a registry, but nobody covers this county" are different claims, and a scorer has to
    be able to make both - so the absence has to be visible, not papered over."""
    import inspect

    assert inspect.signature(ScorerContext).parameters["features"].default is None
