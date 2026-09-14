"""Honest coverage reporting (scope 3.6, 9, 12).

"Every plan states which data sources were checked and which were not" is a design
principle, so it cannot be something a scorer returns only when it remembers to. The
manifest is a **sink**: scorers write into the one on their context, and a source that
could not be consulted produces an entry rather than an exception.

The distinction that matters throughout: a missing OSM tag means *unknown*, never
*absent* (scope 12). A scorer that saw no `lit=yes` has not established darkness.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Adapter reliability tiers (scope 7.10). 1 is a standard feed, 4 is LLM extraction.
Tier = Literal[1, 2, 3, 4]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class CoverageEntry(BaseModel):
    """One source, for one jurisdiction, and whether it actually answered."""

    model_config = ConfigDict(frozen=True)

    source: str
    kind: str
    checked: bool
    jurisdiction: str | None = None
    tier: Tier | None = None
    reason: str | None = None
    confidence: Confidence | None = None
    vintage: str | None = None

    def __str__(self) -> str:
        """One line of the plan sheet's coverage section.

        The tier is rendered, not just stored, because scope 7.6 asks a plan to report
        "which tiers returned data for each jurisdiction crossed" — and tiers 3 and 4 are
        brittle by design, so "checked" alone invites a reader to trust an LLM reading a
        PDF exactly as far as they trust a state DOT's feed.
        """
        state = "checked" if self.checked else "NOT CHECKED"
        where = f" [{self.jurisdiction}]" if self.jurisdiction else ""
        how = f" (tier {self.tier})" if self.checked and self.tier is not None else ""
        why = f" - {self.reason}" if self.reason else ""
        return f"{self.source}{where}: {state}{how}{why}"


class CoverageManifest(BaseModel):
    """Accumulates coverage across every scorer in a plan.

    Rendered verbatim into the plan sheet (scope 9), including — especially — the
    sources that returned nothing.
    """

    entries: list[CoverageEntry] = Field(default_factory=list)

    def record(self, entry: CoverageEntry) -> None:
        self.entries.append(entry)

    def checked(self) -> list[CoverageEntry]:
        return [e for e in self.entries if e.checked]

    def unchecked(self) -> list[CoverageEntry]:
        """The half of the manifest that exists to be reported, not hidden."""
        return [e for e in self.entries if not e.checked]

    def render(self) -> str:
        if not self.entries:
            return "No sources recorded."
        lines = [f"- {e}" for e in self.entries]
        return "\n".join(lines)
