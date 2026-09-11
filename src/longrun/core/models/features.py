"""The common schema every jurisdiction adapter returns (scope 7.10).

One shape for closures, trail status, access hours and speed surveys, whether it came
from a WZDx feed or an LLM reading a PDF. `confidence` and `tier` travel with the record
so the plan sheet can say how much to trust it: tier-4 extraction is capped at 0.5 and
marked unverified in the coverage manifest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from longrun.core.models.coverage import Confidence, CoverageEntry, Tier

FeatureKind = Literal["closures", "trail_status", "access_hours", "speed_survey"]

#: Above this, a record must have come from a structured feed rather than extraction.
MAX_EXTRACTION_CONFIDENCE = 0.5


class Feature(BaseModel):
    """One record from one adapter."""

    model_config = ConfigDict(frozen=True)

    kind: FeatureKind
    category: str
    geometry: dict[str, Any]
    tier: Tier
    confidence: Confidence
    jurisdiction: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    source_url: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _check_extraction_confidence(self) -> Feature:
        if self.tier == 4 and self.confidence > MAX_EXTRACTION_CONFIDENCE:
            raise ValueError(
                f"tier-4 extraction cannot claim confidence above "
                f"{MAX_EXTRACTION_CONFIDENCE}; got {self.confidence}"
            )
        if self.start and self.end and self.end < self.start:
            raise ValueError("feature end precedes its start")
        return self

    def active_at(self, when: datetime) -> bool:
        """Whether this record applies at a given instant. Open-ended bounds count."""
        if self.start and when < self.start:
            return False
        if self.end and when > self.end:
            return False
        return True


class JurisdictionAnswer(BaseModel):
    """What one jurisdiction, for one kind, actually answered (scope 7.6, 7.10).

    Scope 7.6 requires that "every plan reports which tiers returned data for each
    jurisdiction crossed". Three flat lists of ids cannot say that: they cannot distinguish
    "tier 1 answered", "only tier 4 was available", and "nobody covers this place". This
    record can, and it is what finally fills `CoverageEntry.jurisdiction` and `.tier` —
    two fields that have existed since M1 and been passed by nothing.
    """

    model_config = ConfigDict(frozen=True)

    jurisdiction: str
    kind: FeatureKind
    name: str | None = None
    checked: bool = False
    #: The tier that answered. `None` means none did, which is not the same as tier 4.
    tier: Tier | None = None
    adapter: str | None = None
    #: The coarser jurisdiction whose adapter covered this one. A state DOT's feed answers
    #: for every county and place inside it, and a sheet that left those blank would say
    #: the route crossed places nobody checked when in fact one fetch covered them all.
    covered_by: str | None = None
    count: int = 0
    reason: str | None = None
    vintage: str | None = None
    confidence: Confidence | None = None

    def label(self) -> str:
        """How this jurisdiction is named in the plan sheet.

        Name *and* id, because neither alone is enough. `tiger:place:2938000` is opaque to
        a reader, and "Kansas City" is ambiguous to everyone: there is one in Missouri and
        one in Kansas, they share a border, and scope 11's state-line test region is built
        out of exactly that pair.
        """
        return self.jurisdiction if self.name is None else f"{self.name} ({self.jurisdiction})"

    def coverage(self, source: str) -> CoverageEntry:
        """This answer as a manifest entry."""
        return CoverageEntry(
            source=source,
            kind=self.kind,
            checked=self.checked,
            jurisdiction=self.label(),
            tier=self.tier,
            reason=self.reason,
            confidence=self.confidence,
            vintage=self.vintage,
        )


class FeatureSet(BaseModel):
    """What a registry lookup returns: the records, and who was asked (scope 7.10).

    An empty `features` with a populated `queried` list is a real answer — it means the
    jurisdictions were checked and had nothing. An empty `queried` means nobody was asked,
    which is a different statement entirely and must reach the coverage manifest as such.

    `answers` is the same statement at full resolution, per jurisdiction and per tier.
    `queried` and `missing_adapters` remain real fields rather than becoming properties
    derived from it, because they are the summary a caller usually wants and deriving them
    would make every read walk the list. `from_answers` is what keeps the two consistent —
    the registry never fills them by hand.
    """

    features: list[Feature] = Field(default_factory=list)
    queried: list[str] = Field(default_factory=list)
    missing_adapters: list[str] = Field(default_factory=list)
    answers: list[JurisdictionAnswer] = Field(default_factory=list)

    @classmethod
    def from_answers(cls, features: list[Feature], answers: list[JurisdictionAnswer]) -> FeatureSet:
        """Build a set whose summary fields cannot disagree with its answers."""
        return cls(
            features=features,
            queried=[a.jurisdiction for a in answers if a.checked],
            missing_adapters=[a.jurisdiction for a in answers if not a.checked],
            answers=answers,
        )

    @property
    def answered(self) -> bool:
        """Whether anybody at all was successfully asked."""
        return any(a.checked for a in self.answers) or bool(self.queried)
