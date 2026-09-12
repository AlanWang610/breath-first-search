"""Tier 4 with a model in it (scope 7.10; ADR 0013, 0015).

`NullExtractor` said "M5 owns the model call sites" and this is that. What it does *not*
change is the shape: the registry takes an `Extractor` and this is one, so wiring it is the
single constructor argument `registry.py` said it would be.

**This is the fifth call site and the weakest one, on purpose.** A tier-4 record is an
unverified reading of a page nobody wrote for a machine. Two mechanisms already make that
safe and neither is new here:

* `Feature`'s validator rejects tier 4 above `MAX_EXTRACTION_CONFIDENCE = 0.5`, so nothing
  this produces can clear `MIN_HARD_FLAG_TIER = 2`. A verification gate that could be
  tripped by a model reading a PDF is worse than no gate, and this one provably cannot.
* Every answer is a `CoverageEntry` at tier 4, so a sheet says which tier answered for each
  jurisdiction - scope 7.6's requirement, and the reason the tier reaches the sheet at all.

What is genuinely missing is the *search*: there is no crawler, no page fetcher and no URL
to read. So this extracts from text it is **given** and reports honestly when it is given
none, rather than inventing a source. That is a smaller claim than §7.10's
"search-and-extract" and it is the claim this build can actually support.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from longrun.adapters.base import AdapterContext, AdapterResult
    from longrun.adapters.extraction.seam import ExtractionRequest
    from longrun.core.models.context import Budget

#: The ceiling `Feature` enforces anyway, restated where the value is produced so the two
#: cannot drift apart quietly.
MAX_CONFIDENCE = 0.5

#: Where a page's text would come from, if anything fetched one. Named so the gap is a
#: named gap rather than an absence nobody wrote down.
NO_SOURCE_REASON = (
    "tier-4 extraction has no page to read: nothing in this build searches for or fetches "
    "a jurisdiction's closure page, so there is no text to extract from"
)


class ExtractedClosure(BaseModel):
    """One statement a page made, with its own uncertainty attached."""

    what: str = Field(description="What is closed or restricted, in the page's own terms.")
    where: str | None = Field(None, description="The road, trail or area named.")
    starts: str | None = Field(None, description="ISO date, only if the page gives one.")
    ends: str | None = Field(None, description="ISO date, only if the page gives one.")
    confidence: float = Field(0.3, ge=0.0, le=MAX_CONFIDENCE)
    ambiguity: str | None = Field(None, description="What the page left unclear.")


class Extraction(BaseModel):
    """Everything one page said."""

    closures: list[ExtractedClosure] = Field(default_factory=list)
    says_nothing: bool = Field(False, description="True when the page states no closure.")


class ModelExtractor:
    """An `Extractor` that reads text it is handed.

    `pages` is a callable returning the text for a jurisdiction, or `None`. Injected rather
    than built in, because the thing that would fetch a page does not exist - and a default
    that quietly returned nothing would make this look wired when it is not.
    """

    tier = 4

    def __init__(self, pages: Any = None, budget: Budget | None = None) -> None:
        self.pages = pages
        self.budget = budget

    def extract(self, request: ExtractionRequest, ctx: AdapterContext) -> AdapterResult:
        from longrun.adapters.base import AdapterResult
        from longrun.agent import prompts
        from longrun.agent.model import ModelUnavailable, ask, build_agent

        text = self.pages(request) if self.pages is not None else None
        if not text:
            return AdapterResult(reason=NO_SOURCE_REASON)

        try:
            agent = build_agent(Extraction, prompts.EXTRACTION)
        except ModelUnavailable as exc:
            return AdapterResult(reason=str(exc))

        answer = ask(agent, str(text), budget=self.budget or getattr(ctx, "budget", None))
        if answer is None:
            return AdapterResult(reason="tier-4 extraction returned nothing")
        if answer.says_nothing or not answer.closures:
            # "The page says nothing" is a *checked* answer and a useful one - it is the
            # difference between a jurisdiction nobody asked and one that was asked and had
            # nothing to report (scope 12's three states).
            return AdapterResult(features=[], reason="the page states no closure")

        return AdapterResult(
            features=[self._feature(c, request) for c in answer.closures],
            reason=None,
        )

    def _feature(self, closure: ExtractedClosure, request: ExtractionRequest) -> Any:
        from longrun.core.models.features import Feature

        return Feature(
            kind=request.kind,
            category="extracted",
            geometry=_geometry(request.polygon),
            # Whatever the model claimed, capped. The validator would reject a higher
            # value; capping here means an over-confident answer degrades rather than
            # raising, which is what every other unreliable source in this project does.
            confidence=min(float(closure.confidence), MAX_CONFIDENCE),
            tier=4,
            jurisdiction=request.jurisdiction.id,
            start=_when(closure.starts),
            end=_when(closure.ends),
            detail=_describe(closure),
        )


def _geometry(polygon: Any) -> dict[str, Any]:
    """The polygon the question was asked about, as GeoJSON.

    A tier-4 record has no geometry of its own - a page says "the north trail is closed"
    and means somewhere the reader is expected to know. The jurisdiction's own shape is
    the honest extent, and confidence at 0.5 or below is what says so.
    """
    if isinstance(polygon, dict):
        return polygon
    try:
        from shapely.geometry import mapping

        return dict(mapping(polygon))
    except Exception:  # noqa: BLE001 - a shape that will not map is not worth a failed plan
        return {"type": "GeometryCollection", "geometries": []}


def _when(value: str | None) -> Any:
    """An ISO date the page gave, or nothing. Never a date this invented."""
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _describe(closure: ExtractedClosure) -> str:
    parts = [closure.what]
    if closure.where:
        parts.append(f"at {closure.where}")
    if closure.starts or closure.ends:
        parts.append(f"({closure.starts or '?'} to {closure.ends or '?'})")
    if closure.ambiguity:
        # Carried into the description rather than dropped: an ambiguity the page had is
        # the most useful thing a tier-4 record can tell a reader about itself.
        parts.append(f"- unclear: {closure.ambiguity}")
    return " ".join(parts)


__all__ = ["MAX_CONFIDENCE", "NO_SOURCE_REASON", "Extraction", "ExtractedClosure", "ModelExtractor"]
