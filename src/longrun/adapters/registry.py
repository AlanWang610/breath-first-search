"""Entry-point discovery, jurisdiction lookup, and coverage reporting (scope 7.10).

The registry is what turns "this route crosses Jackson County" into "Missouri DOT's WZDx
feed answered, at tier 1, and here is what it said". It satisfies `core.data.base
.FeatureSource`, so nothing in `core/` knows it exists beyond the shape of its two methods.

**Fan out by adapter, not by jurisdiction.** This is the whole budget story and it is
`alerts.py`'s insight generalised. An NWS alert is issued over a county, so sampling every
5 km asks one zone the same question ten times; a WZDx feed is published for a *state*, so
asking it once per place crossed does the same thing with more HTTP. Matched adapters are
grouped, fetched once each, and the result fanned back out - every jurisdiction the fetch
covered records `covered_by`, so the sheet can say a place was checked without implying a
separate request. A Kansas City route crossing ~12 jurisdictions costs two fetches.

**Ascending tiers, first success wins.** A jurisdiction with a tier-1 feed and a tier-3
portal is asked tier 1 first and stops there. That is `route_forecast`'s NWS -> Open-Meteo
ladder rather than a new policy, and it is why cache keys are scoped per adapter: a tier
that was never reached is not a source that failed, and writing it to the manifest as one
would be a lie of a subtle kind.

**Nothing here raises.** `discover()` turns an unimportable entry point into a
`LoadFailure`; a fetch that throws becomes a reason on the answer. `longrun repair` must
not become an ImportError traceback because somebody's third-party adapter is broken.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from longrun.adapters.base import (
    ENTRY_POINT_GROUP,
    Adapter,
    AdapterContext,
    AdapterResult,
    LoadFailure,
    describe,
    valid_jurisdiction_id,
)
from longrun.adapters.extraction.seam import ExtractionRequest, Extractor, NullExtractor

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.core.data.base import Cache
    from longrun.core.models.context import Budget
    from longrun.core.models.features import (
        Feature,
        FeatureKind,
        FeatureSet,
        JurisdictionAnswer,
    )
    from longrun.core.models.jurisdiction import AdapterInfo, Jurisdiction

#: Adapter fetches one plan may make, across every kind. Named and reportable in the shape
#: of `alerts.MAX_ALERT_SITES`: beyond it, remaining jurisdictions get a reason rather than
#: a silently truncated answer. Fetches are ordered best-tier-first and most-specific-first,
#: so what a ceiling drops is the least useful thing left.
MAX_ADAPTER_FETCHES = 24

#: Jurisdictions one plan resolves against adapters. A route crossing more than this many
#: distinct bodies is a route through a metro area's worth of incorporated places, and
#: asking about all of them costs more than it tells anyone.
MAX_JURISDICTIONS = 40


def discover(group: str = ENTRY_POINT_GROUP) -> tuple[list[Adapter], list[LoadFailure]]:
    """Every registered adapter, and every entry point that would not become one.

    Two kinds of failure are caught and neither raises: a module that will not import, and
    an object that loads but does not satisfy the protocol or declares a malformed
    jurisdiction id. The second is the one worth catching early - a typo in an id has no
    symptom at all, because the adapter loads, matches nothing, and every jurisdiction
    reports "no adapter" exactly as it did before anybody wrote it.
    """
    from importlib.metadata import entry_points

    found: list[Adapter] = []
    failures: list[LoadFailure] = []

    for point in entry_points(group=group):
        try:
            candidate = point.load()
        except Exception as exc:  # noqa: BLE001 - a broken adapter is not a broken plan
            failures.append(LoadFailure(name=point.name, reason=describe(exc)))
            continue
        problem = _rejected(candidate)
        if problem is not None:
            failures.append(LoadFailure(name=point.name, reason=problem))
            continue
        found.append(candidate)

    found.sort(key=lambda a: (a.tier, a.name))
    return found, failures


def _rejected(candidate: Any) -> str | None:
    """Why an object is not usable as an adapter, or `None` if it is."""
    if not isinstance(candidate, Adapter):
        return f"does not satisfy the Adapter protocol (got {type(candidate).__name__})"
    ids = tuple(candidate.jurisdictions)
    if not ids:
        return "declares no jurisdictions, so it can never match"
    bad = [i for i in ids if not valid_jurisdiction_id(i)]
    if bad:
        return f"declares malformed jurisdiction id(s): {', '.join(sorted(bad))}"
    return None


def matches(adapter: Adapter, kind: FeatureKind, jurisdiction: Jurisdiction) -> bool:
    """Whether an adapter answers for a jurisdiction.

    Exact set membership against `Jurisdiction.ids`, which is the jurisdiction plus
    everything containing it. Never a lexical prefix test: county `29095` is a prefix of
    the possible place `2909512`, and containment is declared in `within` precisely so it
    can be checked rather than inferred from the digits.
    """
    return adapter.kind == kind and any(i in adapter.jurisdictions for i in jurisdiction.ids)


class AdapterRegistry:
    """The live `FeatureSource`: entry-point adapters, asked once each."""

    def __init__(
        self,
        cache: Cache,
        budget: Budget,
        *,
        offline: bool = False,
        adapters: Sequence[Adapter] | None = None,
        extractor: Extractor | None = None,
    ) -> None:
        """`adapters` injected bypasses discovery, which is how the tests run.

        Without it every adapter test would depend on the package having been reinstalled
        since `pyproject.toml` last changed - `importlib.metadata` reads installed
        metadata, not the file on disk - and a suite whose result depends on that is a
        suite that fails for reasons nobody can see in the diff.
        """
        self._cache = cache
        self._budget = budget
        self._offline = offline
        self._extractor = extractor if extractor is not None else NullExtractor()
        if adapters is None:
            self._adapters, self.failures = discover()
        else:
            self._adapters, self.failures = sorted(adapters, key=lambda a: (a.tier, a.name)), []
        self._fetches = 0

    # --- FeatureSource -------------------------------------------------------

    def adapters_for(self, kind: FeatureKind, jurisdiction: Jurisdiction) -> list[AdapterInfo]:
        """Who claims this jurisdiction, best tier first. No fetch, no budget spent."""
        from longrun.core.models.jurisdiction import AdapterInfo

        return [
            AdapterInfo(name=a.name, tier=a.tier, kind=a.kind, source=a.source)
            for a in self._adapters
            if matches(a, kind, jurisdiction)
        ]

    def fetch(
        self,
        kind: FeatureKind,
        jurisdictions: Sequence[Jurisdiction],
        polygon: Any,
        day: date,
    ) -> FeatureSet:
        from longrun.core.models.features import FeatureSet, JurisdictionAnswer

        considered = list(jurisdictions)[:MAX_JURISDICTIONS]
        dropped = list(jurisdictions)[MAX_JURISDICTIONS:]

        plan = self._plan(kind, considered)
        features: list[Feature] = []
        answers: list[JurisdictionAnswer] = []
        results: dict[str, AdapterResult] = {}

        for name, (adapter, covered) in plan.items():
            result = self._ask(adapter, polygon, day, covered[0])
            results[name] = result
            if result.answered:
                features.extend(result.features)

        for jurisdiction in considered:
            answers.append(self._answer(kind, jurisdiction, day, plan, results))

        answers.extend(
            JurisdictionAnswer(
                jurisdiction=j.id,
                name=j.name,
                kind=kind,
                reason=f"more than {MAX_JURISDICTIONS} jurisdictions on this route",
            )
            for j in dropped
        )
        return FeatureSet.from_answers(features, answers)

    # --- internals -----------------------------------------------------------

    def _plan(
        self, kind: FeatureKind, jurisdictions: Sequence[Jurisdiction]
    ) -> dict[str, tuple[Adapter, list[Jurisdiction]]]:
        """Which adapter to ask, and which jurisdictions each answer covers.

        One entry per adapter, however many jurisdictions it serves. A jurisdiction matched
        by several tiers appears only under the best one, because the ladder stops at the
        first success and a tier never reached did not fail.

        Keyed by `adapter.name` rather than by the adapter, because an adapter is any object
        satisfying a protocol and **need not be hashable** - a plain mutable dataclass is the
        natural way to write one, and requiring `__hash__` would be a constraint on
        third-party code that nothing in scope 7.10 asks for. The name is already the unique
        identifier and the cache scope, so it is the key that was always available.
        """
        chosen: dict[str, tuple[Adapter, list[Jurisdiction]]] = {}
        for jurisdiction in jurisdictions:
            for adapter in self._adapters:  # already sorted by (tier, name)
                if matches(adapter, kind, jurisdiction):
                    chosen.setdefault(adapter.name, (adapter, []))[1].append(jurisdiction)
                    break
        return chosen

    def _ask(
        self, adapter: Adapter, polygon: Any, day: date, on_behalf_of: Jurisdiction
    ) -> AdapterResult:
        """One adapter, once. Every failure becomes a reason."""
        if self._fetches >= MAX_ADAPTER_FETCHES:
            return AdapterResult(reason=f"adapter fetch ceiling ({MAX_ADAPTER_FETCHES}) reached")
        self._fetches += 1
        ctx = AdapterContext(
            cache=self._cache,
            budget=self._budget,
            offline=self._offline,
            jurisdiction=on_behalf_of,
        )
        try:
            return adapter.fetch(polygon, day, ctx)
        except Exception as exc:  # noqa: BLE001 - one feed's failure is not the route's
            return AdapterResult(reason=describe(exc))

    def _answer(
        self,
        kind: FeatureKind,
        jurisdiction: Jurisdiction,
        day: date,
        plan: dict[str, tuple[Adapter, list[Jurisdiction]]],
        results: dict[str, AdapterResult],
    ) -> JurisdictionAnswer:
        from longrun.core.models.features import JurisdictionAnswer

        for name, (adapter, covered) in plan.items():
            if jurisdiction not in covered:
                continue
            result = results[name]
            asked_for = covered[0]
            return JurisdictionAnswer(
                jurisdiction=jurisdiction.id,
                name=jurisdiction.name,
                kind=kind,
                checked=result.answered,
                tier=adapter.tier if result.answered else None,
                adapter=adapter.name,
                covered_by=asked_for.id if asked_for.id != jurisdiction.id else None,
                count=sum(1 for f in result.features if f.jurisdiction in (None, jurisdiction.id))
                if result.answered
                else 0,
                reason=result.reason,
                vintage=result.vintage,
            )
        return self._extracted(kind, jurisdiction, day)

    def _extracted(
        self, kind: FeatureKind, jurisdiction: Jurisdiction, day: date
    ) -> JurisdictionAnswer:
        """§7.10's fallback: no adapter means tier-4 search-and-extract.

        With `NullExtractor` this is always an honest `checked=False`, which is what the
        jurisdiction would have reported anyway - but it is reported *through* the tier-4
        path, so wiring a real extractor in M5 changes one constructor argument and not the
        shape of a single coverage entry.
        """
        from longrun.core.models.features import JurisdictionAnswer

        result = self._extractor.extract(
            ExtractionRequest(jurisdiction=jurisdiction, kind=kind, polygon=None, day=day),
            AdapterContext(cache=self._cache, budget=self._budget, offline=self._offline),
        )
        return JurisdictionAnswer(
            jurisdiction=jurisdiction.id,
            name=jurisdiction.name,
            kind=kind,
            checked=result.answered,
            tier=4 if result.answered else None,
            adapter="extraction" if result.answered else None,
            count=len(result.features),
            reason=result.reason,
        )


__all__ = [
    "MAX_ADAPTER_FETCHES",
    "MAX_JURISDICTIONS",
    "AdapterRegistry",
    "discover",
    "matches",
]
