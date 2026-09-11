"""Tier 4, without a model in it (scope 7.10).

§7.10 makes tier-4 extraction the fallback for a jurisdiction no adapter covers: *"Missing
adapter → generic tier-4 search-and-extract with confidence ≤0.5 and a manifest entry
marked unverified."* This module is that fallback's shape, and `NullExtractor` is what
occupies it until M5.

**The model is deferred on purpose, and the purpose is not caution about cost.** `agent/`
is where the five fixed LLM call sites live, along with the prompts, their versioning, and
the budget that governs them; M5 builds it. Putting the project's first model call here
would put it outside the layer designed to own it, and `tests/README.md` already scopes
what tier 4 owes a test suite: *"tier-4 extraction is tested for schema and confidence
bounds, not content"* - which is testable now, against no model at all.

What is real here is the bound. `Feature`'s validator rejects tier 4 above confidence 0.5,
so a tier-4 record **provably cannot** produce a hard flag under M4.5's `MIN_HARD_FLAG_TIER`
rule. That is the part worth having early: a verification gate that could be tripped by a
model reading a PDF is worse than no gate, and the model that would do it does not exist
yet in either sense.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult
    from longrun.core.models.features import FeatureKind
    from longrun.core.models.jurisdiction import Jurisdiction


@dataclass(frozen=True)
class ExtractionRequest:
    """What a model would be asked, stated as data so it can be tested without one."""

    jurisdiction: Jurisdiction
    kind: FeatureKind
    polygon: Any
    day: date


@runtime_checkable
class Extractor(Protocol):
    """Search-and-extract for a jurisdiction nobody has written an adapter for."""

    def extract(self, request: ExtractionRequest, ctx: AdapterContext) -> AdapterResult: ...


class NullExtractor:
    """No model wired up: every answer is an honest `checked=False` at tier 4.

    Not a stub in the pejorative sense, by the same argument `unavailable()` carries. A
    jurisdiction with no adapter and no extraction has been checked by nobody, and the plan
    sheet saying so is the truthful form of that answer.
    """

    reason = "tier-4 extraction is not wired up (M5 owns the model call sites)"

    def extract(self, request: ExtractionRequest, ctx: AdapterContext) -> AdapterResult:
        from longrun.adapters.base import AdapterResult

        return AdapterResult(reason=self.reason)


__all__ = ["ExtractionRequest", "Extractor", "NullExtractor"]
