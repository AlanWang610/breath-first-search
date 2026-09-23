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
#:
#: **A tier-4 extraction is a fetch and spends this.** It did not until M13.5, which is the
#: bug `_extracted` documents: tier 4 makes a model call per uncovered jurisdiction, and
#: `Budget.model_calls_max` is 12 while `MAX_JURISDICTIONS` is 40.
MAX_ADAPTER_FETCHES = 24

#: Decimal places on the mean confidence a tier-4 answer reports. The coverage manifest goes
#: verbatim into `expected.json`, so an unrounded mean is a golden that fails on a different
#: libm rather than on a different measurement.
EXTRACTION_CONFIDENCE_DP = 3

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
            # Two values, because the loop above collects only what an *adapter* returned.
            # A tier-4 extraction happens inside `_answer` - it has no entry in `plan` and
            # no `AdapterResult` in `results` - so its records had nowhere to go and were
            # dropped on the floor, while `JurisdictionAnswer.count` still counted them.
            # See `_extracted`: this is the other half of M13.5's first bug, and the half
            # that made the polygon fix necessary but not sufficient.
            answer, extracted = self._answer(kind, jurisdiction, day, polygon, plan, results)
            answers.append(answer)
            features.extend(extracted)

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

        One entry per adapter, however many jurisdictions it serves.

        **The ladder is between tiers, not within one.** A jurisdiction is asked at the best
        tier that covers it and no worse - a tier never reached did not fail, and reporting
        it as though it had would misstate what was consulted. But *every* adapter at that
        best tier is asked, because two sources of the same kind are peers rather than a
        fallback, and stopping at the first would pick between them by sort order.

        Arizona is why this is not hypothetical. `wzdx.maricopa` (the county) and
        `wzdx.azdot` (the state) are both tier-1 WZDx with **disjoint `data_sources`**, so
        each carries work zones the other does not. Under a first-match rule Phoenix silently
        got whichever name sorted first - and since MCDOT publishes
        `vehicle_impact: "unknown"` on every feature while AZDOT states it properly, that
        accident decided whether an Arizona closure could clear ADR 0013's gate 1 at all.

        Keyed by `adapter.name` rather than by the adapter, because an adapter is any object
        satisfying a protocol and **need not be hashable** - a plain mutable dataclass is the
        natural way to write one, and requiring `__hash__` would be a constraint on
        third-party code that nothing in scope 7.10 asks for. The name is already the unique
        identifier and the cache scope, so it is the key that was always available.
        """
        chosen: dict[str, tuple[Adapter, list[Jurisdiction]]] = {}
        for jurisdiction in jurisdictions:
            matching = [a for a in self._adapters if matches(a, kind, jurisdiction)]
            if not matching:
                continue
            best = min(int(a.tier) for a in matching)
            for adapter in matching:
                if int(adapter.tier) == best:
                    chosen.setdefault(adapter.name, (adapter, []))[1].append(jurisdiction)
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
        polygon: Any,
        plan: dict[str, tuple[Adapter, list[Jurisdiction]]],
        results: dict[str, AdapterResult],
    ) -> tuple[JurisdictionAnswer, list[Feature]]:
        """One jurisdiction's answer, and any records only this call has seen.

        The second value is empty for every jurisdiction an adapter covered, because
        `fetch` collected those from the `AdapterResult` before this ran. It is non-empty
        only on the tier-4 path, which performs its own fetch here and is otherwise the one
        source whose records nothing downstream would ever receive.
        """
        from longrun.core.models.features import JurisdictionAnswer

        asked = [
            (adapter, covered, results[name])
            for name, (adapter, covered) in plan.items()
            if jurisdiction in covered
        ]
        if not asked:
            return self._extracted(kind, jurisdiction, day, polygon)

        answered = [entry for entry in asked if entry[2].answered]
        if not answered:
            # Every adapter covering this jurisdiction failed. Their reasons are different
            # facts - one key missing, one feed down - so they are joined rather than
            # reduced to the first, which is what a reader needs to act on either.
            return (
                JurisdictionAnswer(
                    jurisdiction=jurisdiction.id,
                    name=jurisdiction.name,
                    kind=kind,
                    checked=False,
                    adapter="; ".join(a.name for a, _, _ in asked),
                    reason="; ".join(
                        dict.fromkeys(r.reason or "no reason given" for _, _, r in asked)
                    ),
                ),
                [],
            )

        count = 0
        for _, _, result in answered:
            count += sum(1 for f in result.features if f.jurisdiction in (None, jurisdiction.id))
        first_covered = answered[0][1][0]
        return (
            JurisdictionAnswer(
                jurisdiction=jurisdiction.id,
                name=jurisdiction.name,
                kind=kind,
                checked=True,
                tier=min(int(a.tier) for a, _, _ in answered),  # type: ignore[arg-type]
                adapter="; ".join(a.name for a, _, _ in answered),
                covered_by=first_covered.id if first_covered.id != jurisdiction.id else None,
                count=count,
                vintage="; ".join(dict.fromkeys(r.vintage for _, _, r in answered if r.vintage))
                or None,
            ),
            [],
        )

    def _extracted(
        self, kind: FeatureKind, jurisdiction: Jurisdiction, day: date, polygon: Any
    ) -> tuple[JurisdictionAnswer, list[Feature]]:
        """§7.10's fallback: no adapter means tier-4 search-and-extract.

        With `NullExtractor` this is always an honest `checked=False`, which is what the
        jurisdiction would have reported anyway - but it is reported *through* the tier-4
        path, so wiring a real extractor in M5 changes one constructor argument and not the
        shape of a single coverage entry.

        **Everything this passes on was wrong while the path was inert** (M13.5). None of it
        had a symptom, because `ModelExtractor` had no instantiation anywhere and
        `NullExtractor` reaches none of it - which is exactly why it was worth fixing before
        a live extractor made it visible as bad output rather than as a bug.

        *The records themselves.* This returns them now. `fetch` collects features from the
        `AdapterResult`s in its `plan` loop, and a tier-4 extraction has no entry in `plan`
        and no `AdapterResult` in `results` - so its records went nowhere at all, while
        `count` below still counted them. That is the *actual* mechanism behind "tier 4
        answered, 3 records, zero flags": not a geometry that `closures` rejected, but
        records `closures` never received. Worth stating plainly, because the geometry bug
        below looks like a sufficient explanation and is not.

        *The corridor polygon.* This used to pass `polygon=None`. A tier-4 record has no
        geometry of its own, so `model._geometry` falls back to the shape it is handed, and
        `None` produces an empty `GeometryCollection`: `runs_along` then returns `inf` and
        `closures.py` drops the feature for being off-route. So the records that now escape
        would have been discarded on arrival. Two bugs, one symptom, and fixing either alone
        would have left the symptom exactly where it was.

        *The fetch ceiling.* `_ask` checks and increments `MAX_ADAPTER_FETCHES`; this did
        neither, so tier 4 ran outside the budget `Budget.model_calls_max`'s own comment
        claims it lives behind ("tier-4 extraction is capped separately, by the adapter
        fetch limits it already lives behind"). A route crossing forty uncovered
        jurisdictions would have made up to forty model calls against a twelve-call
        ceiling, and `ask` returns `None` on `BudgetExceeded` rather than raising - so calls
        thirteen onward read as "extraction returned nothing", which is a sentence about the
        page rather than about the budget. Checked before the call and reported by name,
        the same way `_ask` does it, and counted on the same counter because they are the
        same budget.

        *The confidence.* §7.10 asks for "a manifest entry marked unverified" and this
        conveyed it by `tier=4` alone, leaving `JurisdictionAnswer.confidence` - which
        `CoverageEntry` carries and the plan sheet prints - empty on every tier-4 answer.
        It is the **mean** over the records rather than the maximum: one confident line must
        not speak for a page of vague ones, and mean is how every other confidence in this
        project is summarised. Rounded, because the coverage manifest is serialized verbatim
        into a golden expectation and an unrounded mean is cross-platform float noise.

        An answer that carries no records keeps `confidence=None`, and that is not an
        oversight. "The page states no closure" is a checked answer with nothing to attach a
        number to, and a confidence invented for it would be a number about nothing - the
        tier still says the reading is unverified.
        """
        from longrun.core.models.features import JurisdictionAnswer

        if self._fetches >= MAX_ADAPTER_FETCHES:
            return (
                JurisdictionAnswer(
                    jurisdiction=jurisdiction.id,
                    name=jurisdiction.name,
                    kind=kind,
                    reason=f"adapter fetch ceiling ({MAX_ADAPTER_FETCHES}) reached",
                ),
                [],
            )
        self._fetches += 1
        result = self._extractor.extract(
            ExtractionRequest(jurisdiction=jurisdiction, kind=kind, polygon=polygon, day=day),
            AdapterContext(cache=self._cache, budget=self._budget, offline=self._offline),
        )
        confidences = [float(f.confidence) for f in result.features]
        return (
            JurisdictionAnswer(
                jurisdiction=jurisdiction.id,
                name=jurisdiction.name,
                kind=kind,
                checked=result.answered,
                tier=4 if result.answered else None,
                adapter="extraction" if result.answered else None,
                count=len(result.features),
                reason=result.reason,
                confidence=(
                    round(sum(confidences) / len(confidences), EXTRACTION_CONFIDENCE_DP)
                    if confidences
                    else None
                ),
            ),
            list(result.features) if result.answered else [],
        )


__all__ = [
    "MAX_ADAPTER_FETCHES",
    "MAX_JURISDICTIONS",
    "AdapterRegistry",
    "discover",
    "matches",
]
