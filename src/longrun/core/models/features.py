"""The common schema every jurisdiction adapter returns (scope 7.10).

One shape for closures, trail status, access hours and speed surveys, whether it came
from a WZDx feed or an LLM reading a PDF. `confidence` and `tier` travel with the record
so the plan sheet can say how much to trust it: tier-4 extraction is capped at 0.5 and
marked unverified in the coverage manifest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    #: **Naive UTC, always** (ADR 0046): the window is an instant, and every instant this
    #: project stores is naive UTC (ADR 0045). An ETA is naive *local*; read one against the
    #: other through `core.data.features.RouteFeatures`, never directly.
    start: datetime | None = None
    end: datetime | None = None
    source_url: str | None = None
    detail: str | None = None

    @field_validator("start", "end", mode="after")
    @classmethod
    def _naive_utc(cls, value: datetime | None) -> datetime | None:
        """An aware stamp is converted, not refused and not stripped.

        The backstop behind every adapter's own parsing. Stripping is the bug ADR 0045 and
        ADR 0046 both record, and refusing would turn one publisher's offset into a feed
        that answers nothing.
        """
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

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
        """Whether this record applies at a given naive-UTC instant. Open-ended bounds count.

        A scorer holding a local ETA asks `RouteFeatures.active_at` instead, which converts.
        """
        if self.start and when < self.start:
            return False
        if self.end and when > self.end:
            return False
        return True


#: What happened when the registry asked one adapter about one jurisdiction (M17).
#: `skipped`: its key is not set and nothing was recorded to replay, so nothing was tried.
#: `ceiling`: the plan's fetch ceiling for this kind was reached before it could be asked.
AttemptOutcome = Literal["answered", "failed", "skipped", "ceiling"]


class Attempt(BaseModel):
    """One rung of a ladder, as climbed."""

    model_config = ConfigDict(frozen=True)

    tier: Tier
    adapter: str
    outcome: AttemptOutcome
    reason: str | None = None


def render_attempts(attempts: tuple[Attempt, ...], *, checked: bool) -> str | None:
    """The one sentence a coverage entry carries about everything that did not answer.

    **A single tier of failures reads exactly as it always has** - the reasons themselves,
    deduplicated and joined - so a jurisdiction that was only ever asked one question
    reports that question's answer and nothing more. More than one tier names each, in the
    order climbed: `tier 1 wzdx.sfbay: <key reason>. Then tier 4 extraction: <reason>`.
    ". Then " rather than "; " because a key reason already contains a semicolon.

    A *checked* answer names its failures the long way even when there is one, because
    there the failure is not the answer - "peer wzdx.maricopa: not in the cassette" on a
    jurisdiction AZDOT answered is a different claim from the bare reason alone.
    """
    failed = [a for a in attempts if a.outcome != "answered"]
    if not failed:
        return None
    tiers: dict[int, list[Attempt]] = {}
    for attempt in failed:
        tiers.setdefault(int(attempt.tier), []).append(attempt)
    if len(tiers) == 1 and not checked:
        return "; ".join(dict.fromkeys(a.reason or "no reason given" for a in failed))
    parts = []
    for tier, group in sorted(tiers.items()):
        names = ", ".join(dict.fromkeys(a.adapter for a in group))
        reasons = "; ".join(dict.fromkeys(a.reason or "no reason given" for a in group))
        parts.append(f"tier {tier} {names}: {reasons}")
    return ". Then ".join(parts)


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
    #: Every adapter asked, in the order the ladder was climbed (M17). `reason` is rendered
    #: from these by `render_attempts`; they are kept whole so a caller can tell a skipped
    #: key from a failed feed without parsing the sentence.
    attempts: tuple[Attempt, ...] = ()

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
