"""Who owns the ground a route crosses (scope 7.10, 13 step 4).

An adapter claims jurisdictions; a route crosses them; the registry matches the two. This
module is the vocabulary both sides agree on, and it lives in `core/models/` rather than in
`adapters/` because `regions/build.py` resolves jurisdictions with a live database and no
adapter in sight, while a scorer resolves them from a frozen fixture with no database. Two
callers, two queries, one set of records — which is the only thing that stops them drifting.

**An id is a string with a grammar, and the grammar is load-bearing.** `tiger:county:29095`
and `padus:NPS` are the two shapes. Matching is exact set membership against
`Jurisdiction.ids`, never a prefix test: county `29095` is a lexical prefix of a possible
place `2909512`, so `startswith` would silently claim a county adapter covers a city inside
it. Containment is declared in `within`, where it can be checked, rather than inferred from
the digits.

**PAD-US `Mang_Name` is a coded domain, not an agency name**, and this is the subtlety the
layer's own docstring understates. The committed fixtures hold `CITY`, `JNT`, `NGO`, `NPS`,
`PVT`, `UNK`, `SDOL`. Only the federal codes name one agency nationally — `NPS` is the
National Park Service everywhere. `CITY` names a *class* of manager, so `padus:CITY` would
resolve every municipal park in America to one adapter. Non-federal codes therefore carry
the state: `padus:CITY:06`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from longrun.core.models.coverage import Tier
from longrun.core.models.features import FeatureKind

#: TIGER's three nesting levels, plus the one PAD-US contributes.
JurisdictionLevel = Literal["state", "county", "place", "park"]

#: PAD-US `Mang_Name` codes that identify one agency nationally. Everything else — `CITY`,
#: `CNTY`, `SDOL`, `REG`, `NGO`, `PVT`, `UNK` — names a class of manager, and an id built
#: from one of those has to carry the state or it claims the whole country.
FEDERAL_AGENCY_CODES = frozenset(
    {"NPS", "USFS", "FWS", "BLM", "BOR", "DOD", "TVA", "ACE", "NRCS", "OTHF", "USBR"}
)

#: Agency codes that name nobody at all. PAD-US uses several spellings of "unknown", and a
#: park managed by nobody-in-particular must not become a jurisdiction an adapter can claim.
UNKNOWN_AGENCY_CODES = frozenset({"", "UNK", "UNKL", "UNKN", "OTHR", "UNKNOWN"})


class Jurisdiction(BaseModel):
    """One body that governs part of a route, and what it is nested inside."""

    model_config = ConfigDict(frozen=True)

    #: `tiger:{level}:{geoid}` or `padus:{CODE}` / `padus:{CODE}:{statefp}`.
    id: str
    level: JurisdictionLevel
    name: str
    #: Coarser jurisdictions containing this one, as ids. A state DOT's WZDx feed answers
    #: for every county and place inside the state, and this is how the registry knows
    #: that without doing arithmetic on GEOIDs.
    within: tuple[str, ...] = ()
    #: PAD-US `Mang_Name`, which is a *code*. `None` for a census boundary.
    agency: str | None = None
    agency_type: str | None = None
    source: Literal["tiger", "padus"] = "tiger"

    @property
    def ids(self) -> tuple[str, ...]:
        """This jurisdiction and everything it sits inside, for adapter matching."""
        return (self.id, *self.within)


class AdapterInfo(BaseModel):
    """An adapter's declaration, without the adapter.

    `regions/build.py` step 4 asks "which adapters exist for each jurisdiction crossed" and
    has no business importing `adapters/` to find out — scope 13 ends with "the only
    California-specific code in the repository lives in adapters registered for California
    jurisdictions", and a region build that imported one would be a second place.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    tier: Tier
    kind: FeatureKind
    source: str


def tiger_id(level: str, geoid: str) -> str:
    return f"tiger:{level}:{geoid}"


def padus_id(agency: str, statefp: str | None = None) -> str:
    """A PAD-US agency's id, carrying the state unless the code is federal.

    A federal code with a state would fragment one agency into fifty — an adapter for the
    National Park Service covers Yosemite and Acadia alike.
    """
    code = agency.strip().upper()
    if code in FEDERAL_AGENCY_CODES or statefp is None:
        return f"padus:{code}"
    return f"padus:{code}:{statefp}"


def is_unknown_agency(agency: str | None) -> bool:
    """Whether a PAD-US agency code names anybody at all."""
    return agency is None or agency.strip().upper() in UNKNOWN_AGENCY_CODES


__all__ = [
    "FEDERAL_AGENCY_CODES",
    "UNKNOWN_AGENCY_CODES",
    "AdapterInfo",
    "Jurisdiction",
    "JurisdictionLevel",
    "is_unknown_agency",
    "padus_id",
    "tiger_id",
]
