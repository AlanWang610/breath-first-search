"""What an adapter is, and what it may reach for (scope 7.10).

An adapter declares a jurisdiction, a kind and a tier, and answers `fetch(polygon, day)`
with features in the common schema. That schema is `core/models/features.py` and is *not*
redefined here, despite this module's planned description saying "Feature schema": the
layering arrow runs `core/ -> adapters/`, so an adapter imports the vocabulary rather than
publishing a second copy of it.

**An adapter never raises.** A feed that is down, a schema that drifted, a key that is not
set and a jurisdiction with nothing to report are all `AdapterResult`s carrying a reason.
This is the same rule ADR 0006 and ADR 0012 arrived at for AirNow and HPMS, applied where
it matters most: tiers 3 and 4 are brittle by design, and a brittle source that could take
down a plan would make the whole tiered design worse than having no adapter at all.

**`AdapterContext` is deliberately not a `ScorerContext`.** An adapter gets a cache, a
budget and the offline flag. It does not get `layers`, because an adapter reading the
layer store would be reimplementing the store seam from the wrong side of it, and it does
not get `profile`, because an adapter that knew the user's preferences could attach a sign
to what it returns - which scope 3.2 reserves for the preference profile alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.core.data.base import Cache
    from longrun.core.models.context import Budget
    from longrun.core.models.coverage import Tier
    from longrun.core.models.features import Feature, FeatureKind
    from longrun.core.models.jurisdiction import Jurisdiction

#: The entry-point group adapters register under. Third-party packages use the same name,
#: which is the point: discovery is by entry point, never by import path.
ENTRY_POINT_GROUP = "longrun.adapters"

#: `tiger:{level}:{geoid}` or `padus:{CODE}` / `padus:{CODE}:{statefp}`.
#:
#: Validated at discovery rather than trusted, because a typo in an id is the one adapter
#: bug with no symptom: the adapter loads, matches nothing, and every jurisdiction reports
#: "no adapter" exactly as it did before the adapter was written.
_ID_PATTERN = re.compile(r"^(tiger:(state|county|place):\d{2,7}|padus:[A-Z0-9]{2,8}(:\d{2})?)$")


def valid_jurisdiction_id(value: str) -> bool:
    """Whether an id conforms to the grammar `Jurisdiction` mints."""
    return bool(_ID_PATTERN.match(value))


@dataclass(frozen=True)
class AdapterContext:
    """Everything an adapter may reach for, and nothing else."""

    cache: Cache
    budget: Budget
    offline: bool = False
    #: The jurisdiction being asked about, when one adapter serves several.
    jurisdiction: Jurisdiction | None = None


@dataclass(frozen=True)
class AdapterResult:
    """One adapter's answer. Never an exception.

    `features` empty with `reason` `None` is a real answer - the feed was read and had
    nothing in this polygon. `features` empty *with* a reason is the opposite claim. The
    registry turns the difference into `JurisdictionAnswer.checked`, and the whole coverage
    manifest rests on the two not being confused.
    """

    features: list[Feature] = field(default_factory=list)
    reason: str | None = None
    vintage: str | None = None
    source_url: str | None = None

    @property
    def answered(self) -> bool:
        return self.reason is None


@runtime_checkable
class Adapter(Protocol):
    """A jurisdiction-specific source behind one interface.

    An adapter is any object with these members - it need not subclass anything, and in
    particular **it need not be hashable**. A plain mutable dataclass is the natural way to
    write one, so the registry groups adapters by `name` rather than by identity; requiring
    `__hash__` would be a constraint on third-party code that scope 7.10 never asks for.
    """

    #: Dotted, and also the cache tool scope: `wzdx.modot` caches under `adapter.wzdx.modot`.
    name: str
    kind: FeatureKind
    tier: Tier
    #: Jurisdiction ids this adapter answers for. A statewide feed declares the state and
    #: covers every county and place inside it through `Jurisdiction.within`.
    jurisdictions: tuple[str, ...]
    #: The coverage `source` string, and therefore the `attribution.LICENCES` key.
    source: str

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult: ...


@dataclass(frozen=True)
class LoadFailure:
    """An entry point that would not load, reported rather than raised.

    A third-party adapter missing a dependency, or `extraction` without the `agent` extra,
    must not turn `longrun repair` into an ImportError traceback. The plan still runs; the
    jurisdictions that adapter would have covered report why they did not.
    """

    name: str
    reason: str


def describe(exc: BaseException) -> str:
    """A failure named for a coverage manifest.

    Never carries the cache key: two jurisdictions missing from the same cassette are one
    fact to a reader, and including the args hash makes them two strings that cannot be
    deduplicated. `forecast.py` settled this and the wording is deliberately identical.
    """
    from longrun.core.data.cache import CacheMiss
    from longrun.core.models.context import BudgetExceeded

    if isinstance(exc, CacheMiss):
        return "not in the cassette"
    if isinstance(exc, BudgetExceeded):
        return "API call budget exhausted"
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "ENTRY_POINT_GROUP",
    "Adapter",
    "AdapterContext",
    "AdapterResult",
    "LoadFailure",
    "describe",
    "valid_jurisdiction_id",
]
