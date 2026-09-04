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

from longrun.core.models.coverage import Confidence, Tier

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


class FeatureSet(BaseModel):
    """What a registry lookup returns: the records, and who was asked (scope 7.10).

    An empty `features` with a populated `queried` list is a real answer — it means the
    jurisdictions were checked and had nothing. An empty `queried` means nobody was asked,
    which is a different statement entirely and must reach the coverage manifest as such.
    """

    features: list[Feature] = Field(default_factory=list)
    queried: list[str] = Field(default_factory=list)
    missing_adapters: list[str] = Field(default_factory=list)
