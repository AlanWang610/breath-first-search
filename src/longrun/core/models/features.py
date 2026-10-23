"""The common schema every jurisdiction adapter returns (scope 7.10).

One shape for closures, trail status, access hours and speed surveys, whether it came
from a WZDx feed or an LLM reading a PDF. `confidence` and `tier` travel with the record
so the plan sheet can say how much to trust it: tier-4 extraction is capped at 0.5 and
marked unverified in the coverage manifest.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from longrun.core.models.coverage import Confidence, CoverageEntry, Tier

FeatureKind = Literal["closures", "trail_status", "access_hours", "speed_survey"]

#: Above this, a record must have come from a structured feed rather than extraction.
MAX_EXTRACTION_CONFIDENCE = 0.5

#: The one measure every speed survey must carry, and the bounds it is believed within. A
#: walking pace at the bottom and nothing on a public road at the top: a value outside is a
#: unit error (mph read as km/h is still inside, which is why the adapter states its unit).
SPEED_85 = "speed_85_kph"
SPEED_85_RANGE_KPH = (5.0, 200.0)


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
    #: The publisher's own id for the record, where it has one (a WZDx feature `id`). For
    #: tracing a flag back to its source row; never used to match records across two
    #: publishers, whose id spaces are their own.
    ref: str | None = None
    #: An opening-hours rule in OSM `opening_hours` syntax - the subset
    #: `core/scorers/_hours.py` reads - on the **local** clock of the place, and never
    #: converted (ADR 0046 §5): "06:00-22:00" means 06:00 on either side of a DST change.
    #: Only an `access_hours` record carries one, and on that kind `start`/`end` say when
    #: the *rule* holds (a season, a dated exception), not when the gate is open.
    hours: str | None = None
    #: Measured values by name. Only a `speed_survey` record carries them, and it must
    #: carry `speed_85_kph` (M18).
    measures: dict[str, float] | None = None
    #: The day a measurement was taken. A date and not an instant: nothing compares it
    #: against an ETA, and a count taken on 2024-05-07 was taken on that date everywhere.
    observed_on: date | None = None
    #: What the publisher calls the thing - a gate, a road - where it says. Used to match a
    #: survey to a way and to group a gate's rules; never shown as the record's identity.
    name: str | None = None

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

    @model_validator(mode="after")
    def _check_kind_fields(self) -> Feature:
        """A field that belongs to one kind is refused on every other.

        `hours` on a closure would be read by nobody, and a closure that silently lost the
        rule it was published with is worse than one that failed to parse: the second says
        so. The same holds for a measure on anything but a survey.
        """
        if self.hours is not None:
            if self.kind != "access_hours":
                raise ValueError(f"only an access_hours record carries hours, not {self.kind}")
            if not self.hours.strip():
                raise ValueError("an hours rule cannot be blank")
        elif self.kind == "access_hours":
            # Since M18.7 the scorer reads the rule from `hours` and treats `start`/`end` as
            # when the rule holds; a record with no rule would read as a gate always open.
            raise ValueError("an access_hours record carries its rule in hours")
        if self.kind == "speed_survey":
            _check_survey(self.measures, self.geometry)
        elif self.measures is not None:
            raise ValueError(f"only a speed_survey record carries measures, not {self.kind}")
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


def _check_survey(measures: dict[str, float] | None, geometry: dict[str, Any]) -> None:
    """A speed survey is a speed along a line, or it is nothing the build can merge.

    The line is required because the region build matches a survey to a way by distance
    *and* bearing (M18); a point has no bearing, and a count station dropped onto a
    crossroads would be merged into whichever of the two streets happened to be nearer.
    """
    if not measures or SPEED_85 not in measures:
        raise ValueError(f"a speed_survey record must carry measures[{SPEED_85!r}]")
    for key, value in measures.items():
        if not math.isfinite(value):
            raise ValueError(f"measure {key!r} is not a finite number")
    low, high = SPEED_85_RANGE_KPH
    if not low <= measures[SPEED_85] <= high:
        raise ValueError(
            f"{SPEED_85} {measures[SPEED_85]} is outside {low:.0f}-{high:.0f} km/h; "
            f"check the publisher's unit"
        )
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "LineString":
        parts = [coords]
    elif kind == "MultiLineString":
        parts = list(coords)
    else:
        raise ValueError(f"a speed_survey record needs a line geometry, not {kind or 'none'}")
    if not any(len(part) >= 2 for part in parts):
        raise ValueError("a speed_survey line needs at least two coordinates")


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
            covered_by=self.covered_by,
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
