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

**Ascending tiers, first success wins, per claimant** (ADR 0047). Every id a jurisdiction
sits in - itself, its county, its state - is a ladder of whoever claims it, climbed from
tier 1. An answer stops a ladder, including an empty one; a failure falls through to the
next tier; a jurisdiction no ladder answered goes to tier 4. That is `route_forecast`'s NWS
-> Open-Meteo ladder rather than a new policy, and it is why cache keys are scoped per
adapter: a tier that was never reached is not a source that failed, and writing it to the
manifest as one would be a lie of a subtle kind. Until M17 only the best covering tier was
asked at all, so a feed that was down stopped the climb instead of starting it.

**Nothing here raises.** `discover()` turns an unimportable entry point into a
`LoadFailure`; a fetch that throws becomes a reason on the answer. `longrun repair` must
not become an ImportError traceback because somebody's third-party adapter is broken.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from longrun.adapters.base import (
    ENTRY_POINT_GROUP,
    Adapter,
    AdapterContext,
    AdapterResult,
    LoadFailure,
    describe,
    facet_of,
    is_complete,
    key_of,
    scope_of,
    valid_jurisdiction_id,
)
from longrun.adapters.extraction.seam import ExtractionRequest, Extractor, NullExtractor

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.core.data.base import Cache
    from longrun.core.models.context import Budget
    from longrun.core.models.features import (
        Attempt,
        AttemptOutcome,
        Feature,
        FeatureKind,
        FeatureSet,
        JurisdictionAnswer,
    )
    from longrun.core.models.jurisdiction import AdapterInfo, Jurisdiction

#: Adapter fetches one plan may make **per kind** (M17; it was 24 shared across kinds).
#: Named and reportable in the shape of `alerts.MAX_ALERT_SITES`: beyond it, remaining
#: jurisdictions get a reason rather than a silently truncated answer, and the budget gets
#: a degradation line. Each tier's fetches run most-covering-first, so what a ceiling drops
#: is the adapter that answers for least.
#:
#: Per kind because one pool let scorer order decide which kind starved - `closures` runs
#: after `segment_hostility` and before `trail_status`, and with four kinds each adding
#: sources a shared pool would ration closures by accident. Only live asks count: a
#: memoized answer, a missing key's peek and `NullExtractor` cost nothing.
#:
#: **A live tier-4 extraction is a fetch and spends this.** It did not until M13.5, which is
#: the bug `_extracted` documents: tier 4 makes a model call per uncovered jurisdiction.
MAX_ADAPTER_FETCHES = 16

#: Decimal places on the mean confidence a tier-4 answer reports. The coverage manifest goes
#: verbatim into `expected.json`, so an unrounded mean is a golden that fails on a different
#: libm rather than on a different measurement.
EXTRACTION_CONFIDENCE_DP = 3

#: Jurisdictions one plan resolves against adapters. A route crossing more than this many
#: distinct bodies is a route through a metro area's worth of incorporated places, and
#: asking about all of them costs more than it tells anyone.
MAX_JURISDICTIONS = 40


#: An adapter's name is its cache scope (`adapter.wzdx.modot`) and the key the registry
#: groups by, so it has a grammar as load-bearing as a jurisdiction id's: dotted, lower
#: case, stable. A catalog adapter's name is minted from data, which is why it is checked.
_NAME_PATTERN = re.compile(r"^[a-z0-9_]+([.][a-z0-9_-]+)+$")


def discover(group: str = ENTRY_POINT_GROUP) -> tuple[list[Adapter], list[LoadFailure]]:
    """Every registered adapter, and every entry point that would not become one.

    Three kinds of failure are caught and none raises: a module that will not import, an
    object that loads but is not a usable adapter, and a **second adapter with a name
    already taken**. The last used to be silent: `_plan` keyed by name, so the second one
    was never fetched while its jurisdictions were reported covered. Entry points are
    walked in name order, so which of two duplicates wins does not depend on install order.
    """
    from importlib.metadata import entry_points

    found: list[Adapter] = []
    failures: list[LoadFailure] = []
    owners: dict[str, str] = {}

    for point in sorted(entry_points(group=group), key=lambda p: p.name):
        try:
            candidate = point.load()
        except Exception as exc:  # noqa: BLE001 - a broken adapter is not a broken plan
            failures.append(LoadFailure(name=point.name, reason=describe(exc)))
            continue
        adapters, rejected = expand(point.name, candidate)
        failures.extend(rejected)
        for adapter in adapters:
            if adapter.name in owners:
                failures.append(
                    LoadFailure(
                        name=f"{point.name}:{adapter.name}",
                        reason=(
                            f"adapter name {adapter.name!r} is already registered by entry "
                            f"point {owners[adapter.name]!r}"
                        ),
                    )
                )
                continue
            owners[adapter.name] = point.name
            found.append(adapter)

    found.sort(key=lambda a: (a.tier, a.name))
    return found, failures


def expand(point_name: str, loaded: Any) -> tuple[list[Adapter], list[LoadFailure]]:
    """One entry point's target as adapters: a single adapter, or a list or tuple of them.

    The sequence form is for catalogs - fifty portal datasets described as data are one
    module and one entry point, not fifty lines in `pyproject.toml`. Each element is judged
    alone, so one bad catalog row is one `LoadFailure` naming it rather than a catalog that
    vanishes. An element may already be a `LoadFailure`, which is how a catalog reports a
    row it could not build. Strings, mappings and generators are refused: a string is a
    sequence of characters, and a generator would be consumed by whoever looked first.
    """
    if not isinstance(loaded, (list, tuple)):
        problem = _rejected(loaded)
        if problem is not None:
            return [], [LoadFailure(name=point_name, reason=problem)]
        return [loaded], []
    if not loaded:
        return [], [LoadFailure(name=point_name, reason="declares an empty list of adapters")]

    found: list[Adapter] = []
    failures: list[LoadFailure] = []
    for index, item in enumerate(loaded):
        if isinstance(item, LoadFailure):
            failures.append(item)
            continue
        label = f"{point_name}[{getattr(item, 'name', index)}]"
        problem = _rejected(item)
        if problem is not None:
            failures.append(LoadFailure(name=label, reason=problem))
            continue
        found.append(item)
    return found, failures


def _rejected(candidate: Any) -> str | None:
    """Why an object is not usable as an adapter, or `None` if it is."""
    from typing import get_args

    from longrun.core.models.features import FeatureKind

    if not isinstance(candidate, Adapter):
        return f"does not satisfy the Adapter protocol (got {type(candidate).__name__})"
    if not isinstance(candidate.name, str) or not _NAME_PATTERN.match(candidate.name):
        return f"declares a malformed name {candidate.name!r}; expected e.g. 'wzdx.modot'"
    if candidate.kind not in get_args(FeatureKind):
        return f"declares an unknown kind {candidate.kind!r}"
    if candidate.tier not in (1, 2, 3, 4):
        return f"declares tier {candidate.tier!r}; tiers are 1-4"
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
    """The live `FeatureSource`: entry-point adapters, climbed tier by tier, asked once each."""

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
        # Indexed once by (kind, claimed id), instead of scanning every adapter for every
        # jurisdiction of every fetch - which was fine at 22 adapters and is not at 200.
        self._claims: dict[tuple[str, str], list[Adapter]] = {}
        for adapter in self._adapters:
            for claimed in adapter.jurisdictions:
                self._claims.setdefault((adapter.kind, claimed), []).append(adapter)
        self._fetches: dict[str, int] = {}
        self._memo: dict[tuple[Any, ...], AdapterResult] = {}
        self._extracted_memo: dict[tuple[str, str, str], AdapterResult] = {}

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

        ladders = {j.id: self._ladders(kind, j) for j in considered}
        results = self._climb(kind, considered, ladders, polygon, day)

        features: list[Feature] = []
        for _, result in results.values():
            if result.answered:
                features.extend(result.features)

        answers: list[JurisdictionAnswer] = []
        for jurisdiction in considered:
            answer, extracted = self._answer(
                kind, jurisdiction, ladders[jurisdiction.id], results, polygon, day
            )
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

    # --- the ladder ----------------------------------------------------------

    def _ladders(self, kind: FeatureKind, jurisdiction: Jurisdiction) -> list[list[Adapter]]:
        """Every ladder this jurisdiction has: one per (claimed id, facet), best tier first.

        **Per claimant, not per jurisdiction** (ADR 0047). Kansas City's ids are the city,
        its counties and Missouri, and whoever claims each of those climbs a separate
        ladder. With one ladder per jurisdiction a statewide tier-1 feed would pre-empt a
        city's own tier-3 permit portal - the two describe different ground, and the city's
        would never be asked. Arizona is the case that already exists: `wzdx.azdot` claims
        the state and `wzdx.maricopa` the county, with disjoint data sources, and both are
        asked.
        """
        by_ladder: dict[tuple[str, str], list[Adapter]] = {}
        for claimed in jurisdiction.ids:
            for adapter in self._claims.get((kind, claimed), []):
                ladder = by_ladder.setdefault((claimed, facet_of(adapter)), [])
                if all(a.name != adapter.name for a in ladder):
                    ladder.append(adapter)
        return [
            sorted(ladder, key=lambda a: (int(a.tier), a.name)) for ladder in by_ladder.values()
        ]

    def _climb(
        self,
        kind: FeatureKind,
        jurisdictions: Sequence[Jurisdiction],
        ladders: dict[str, list[list[Adapter]]],
        polygon: Any,
        day: date,
    ) -> dict[str, tuple[Adapter, AdapterResult]]:
        """Ask each ladder's rungs in ascending tier until one answers, fanning out by adapter.

        **Falls through on failure, stops on an answer** (ADR 0047). An empty answer is an
        answer - a feed read with nothing in it is evidence - and stops the ladder; a reason
        does not. Until M17 only the best covering tier was ever asked, so a tier-1 feed that
        was down left a jurisdiction unchecked while a working tier-3 portal sat unasked.

        Tiers are walked globally rather than per jurisdiction so each adapter is still
        asked once for everything it covers at that tier: a statewide feed on behalf of
        twelve places is one fetch, which is the whole budget story in the module docstring.
        """
        results: dict[str, tuple[Adapter, AdapterResult]] = {}
        by_id = {j.id: j for j in jurisdictions}
        # Per jurisdiction, the ladders still climbing.
        climbing = {j.id: [ladder for ladder in ladders[j.id] if ladder] for j in jurisdictions}

        for tier in (1, 2, 3, 4):
            asked: dict[str, tuple[Adapter, list[Jurisdiction]]] = {}
            for jid, open_ladders in climbing.items():
                for ladder in open_ladders:
                    for adapter in ladder:
                        if int(adapter.tier) != tier or adapter.name in results:
                            continue
                        covered = asked.setdefault(adapter.name, (adapter, []))[1]
                        if by_id[jid] not in covered:
                            covered.append(by_id[jid])
            # The most useful fetch first: a ceiling should drop the adapter that covers
            # least, not whichever sorted first.
            order = sorted(asked.values(), key=lambda entry: (-len(entry[1]), entry[0].name))
            for adapter, covered in order:
                results[adapter.name] = (adapter, self._ask(kind, adapter, polygon, day, covered))

            for jid, open_ladders in climbing.items():
                still: list[list[Adapter]] = []
                for ladder in open_ladders:
                    rung = [a for a in ladder if int(a.tier) == tier and a.name in results]
                    if any(results[a.name][1].answered and is_complete(a) for a in rung):
                        continue  # answered: this ladder is done
                    if any(_outcome(results[a.name][1]) == "ceiling" for a in rung):
                        continue  # a ceiling ends the ladder; it never falls through
                    if any(int(a.tier) > tier for a in ladder):
                        still.append(ladder)
                climbing[jid] = still
        return results

    def _ask(
        self,
        kind: FeatureKind,
        adapter: Adapter,
        polygon: Any,
        day: date,
        covered: list[Jurisdiction],
    ) -> AdapterResult:
        """One adapter, once per plan for what its answer depends on. Failures are reasons.

        **The memo is what makes the ceiling honest.** One registry serves every scorer and
        every rescoring pass of a plan, and the loop scores each candidate route on the same
        context. Before M17 each pass spent fresh slots on the same answers, so candidates
        scored later saw "ceiling reached" where earlier ones saw closures - and looked
        better in arbitration for it.
        """
        key = _memo_key(adapter, polygon, day, covered)
        if key in self._memo:
            return self._memo[key]

        ctx = AdapterContext(
            cache=self._cache,
            budget=self._budget,
            offline=self._offline,
            jurisdiction=covered[0] if covered else None,
            jurisdictions=tuple(covered),
        )
        # An adapter whose key is not set may still replay what was recorded, so it is
        # asked - but it can only peek, so it spends no slot and meets no ceiling.
        credential = key_of(adapter)
        if credential is None or credential.value() is not None:
            spent = self._fetches.get(kind, 0)
            if spent >= MAX_ADAPTER_FETCHES:
                note = f"adapter fetch ceiling ({MAX_ADAPTER_FETCHES} per kind) reached for {kind}"
                if note not in self._budget.degradation:
                    self._budget.degradation.append(note)
                return AdapterResult(reason=_ceiling_reason())
            self._fetches[kind] = spent + 1
        try:
            result = adapter.fetch(polygon, day, ctx)
        except Exception as exc:  # noqa: BLE001 - one feed's failure is not the route's
            result = AdapterResult(reason=describe(exc))
        self._memo[key] = result
        return result

    def _answer(
        self,
        kind: FeatureKind,
        jurisdiction: Jurisdiction,
        ladders: list[list[Adapter]],
        results: dict[str, tuple[Adapter, AdapterResult]],
        polygon: Any,
        day: date,
    ) -> tuple[JurisdictionAnswer, list[Feature]]:
        """One jurisdiction's answer, and any records only this call has seen.

        The second value is empty unless the tier-4 path ran: `fetch` has already collected
        every adapter's records, and tier 4 performs its own fetch here, so its records
        would otherwise reach nothing downstream (M13.5).
        """
        from longrun.core.models.features import Attempt, JurisdictionAnswer, render_attempts

        climbed: list[tuple[Adapter, AdapterResult]] = []
        for ladder in ladders:
            for adapter in ladder:
                if adapter.name in results and all(a.name != adapter.name for a, _ in climbed):
                    climbed.append(results[adapter.name])
        climbed.sort(key=lambda entry: (int(entry[0].tier), entry[0].name))
        attempts = tuple(
            Attempt(
                tier=adapter.tier,
                adapter=adapter.name,
                outcome=_outcome(result),
                reason=result.reason,
            )
            for adapter, result in climbed
        )
        answered = [(a, r) for a, r in climbed if r.answered]

        if not answered:
            # Tier 4 when no ladder answered - but not past a ceiling, which is a statement
            # about this plan's budget rather than about the jurisdiction's sources.
            if any(a.outcome == "ceiling" for a in attempts):
                return (
                    JurisdictionAnswer(
                        jurisdiction=jurisdiction.id,
                        name=jurisdiction.name,
                        kind=kind,
                        adapter="; ".join(a.adapter for a in attempts),
                        reason=render_attempts(attempts, checked=False),
                        attempts=attempts,
                    ),
                    [],
                )
            return self._extracted(kind, jurisdiction, day, polygon, attempts)

        count = 0
        for _, result in answered:
            count += sum(1 for f in result.features if f.jurisdiction in (None, jurisdiction.id))
        # The id the first answering adapter reached this jurisdiction through: itself, or
        # the county or state whose one fetch covered it.
        through = next(
            (i for i in jurisdiction.ids if i in answered[0][0].jurisdictions), jurisdiction.id
        )
        return (
            JurisdictionAnswer(
                jurisdiction=jurisdiction.id,
                name=jurisdiction.name,
                kind=kind,
                checked=True,
                tier=min(int(a.tier) for a, _ in answered),  # type: ignore[arg-type]
                adapter="; ".join(a.name for a, _ in answered),
                covered_by=through if through != jurisdiction.id else None,
                count=count,
                vintage="; ".join(dict.fromkeys(r.vintage for _, r in answered if r.vintage))
                or None,
                reason=render_attempts(attempts, checked=True),
                attempts=attempts,
            ),
            [],
        )

    def _extracted(
        self,
        kind: FeatureKind,
        jurisdiction: Jurisdiction,
        day: date,
        polygon: Any,
        attempts: tuple[Attempt, ...] = (),
    ) -> tuple[JurisdictionAnswer, list[Feature]]:
        """§7.10's fallback: no ladder answered, so tier-4 search-and-extract.

        With `NullExtractor` this is always an honest `checked=False`, reported *through*
        the tier-4 path so wiring a real extractor changes one constructor argument and not
        the shape of a coverage entry. What M13.5 fixed here stays fixed: the records are
        returned (they have no `AdapterResult` in `fetch`'s loop), the corridor is passed,
        and the confidence is the rounded **mean** over the records - one confident line
        must not speak for a page of vague ones.

        **Since M17 this also runs after every ladder failed**, not only when none existed,
        and its attempt joins theirs in the reason. It is memoized per (jurisdiction, kind,
        day) - never per corridor, which differs by candidate while the page does not - and
        it spends a fetch slot only when the extractor is live: `NullExtractor` reads
        nothing, so charging it was charging for a sentence. A raising extractor is a
        reason, exactly as a raising adapter is in `_ask`.
        """
        from longrun.core.models.features import Attempt, JurisdictionAnswer, render_attempts

        memo = (jurisdiction.id, kind, day.isoformat())
        result = self._extracted_memo.get(memo)
        if result is None:
            live = bool(getattr(self._extractor, "live", True))
            spent = self._fetches.get(kind, 0)
            if live and spent >= MAX_ADAPTER_FETCHES:
                result = AdapterResult(reason=_ceiling_reason())
            else:
                if live:
                    self._fetches[kind] = spent + 1
                try:
                    result = self._extractor.extract(
                        ExtractionRequest(
                            jurisdiction=jurisdiction, kind=kind, polygon=polygon, day=day
                        ),
                        AdapterContext(
                            cache=self._cache,
                            budget=self._budget,
                            offline=self._offline,
                            jurisdiction=jurisdiction,
                            jurisdictions=(jurisdiction,),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - a model is an enrichment
                    result = AdapterResult(reason=describe(exc))
                self._extracted_memo[memo] = result

        attempts = (
            *attempts,
            Attempt(tier=4, adapter="extraction", outcome=_outcome(result), reason=result.reason),
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
                reason=render_attempts(attempts, checked=result.answered),
                confidence=(
                    round(sum(confidences) / len(confidences), EXTRACTION_CONFIDENCE_DP)
                    if confidences
                    else None
                ),
                attempts=attempts,
            ),
            list(result.features) if result.answered else [],
        )


def build_registry(
    cache: Cache,
    budget: Budget,
    *,
    offline: bool = False,
    extractor: Extractor | None = None,
) -> AdapterRegistry:
    """The registry a plan asks and the registry a cassette is recorded through - one
    constructor for both (M17).

    `runtime.open_context` and `freeze-cassette` each built their own, identically for now.
    The day tier 4 is wired into one and not the other, recording and planning ask
    different questions, and a golden replays a cassette that answers none of the plan's.
    """
    return AdapterRegistry(cache, budget, offline=offline, extractor=extractor)


def _ceiling_reason() -> str:
    return f"adapter fetch ceiling ({MAX_ADAPTER_FETCHES}) reached"


def _outcome(result: AdapterResult) -> AttemptOutcome:
    """How an `AdapterResult` reads as a rung of a ladder."""
    if result.answered:
        return "answered"
    if result.key_missing:
        return "skipped"
    if result.reason == _ceiling_reason():
        return "ceiling"
    return "failed"


def _memo_key(
    adapter: Adapter, polygon: Any, day: date, covered: list[Jurisdiction]
) -> tuple[Any, ...]:
    """What an adapter's answer depends on, per its declared scope (`base.scope_of`)."""
    scope = scope_of(adapter)
    if scope == "feed":
        return (adapter.name, day.isoformat())
    if scope == "jurisdictions":
        return (adapter.name, day.isoformat(), tuple(sorted(j.id for j in covered)))
    return (adapter.name, day.isoformat(), _bounds(polygon))


def _bounds(polygon: Any) -> tuple[float, ...] | None:
    """A corridor as a hashable key, at the cache's own coordinate precision."""
    if polygon is None:
        return None
    try:
        return tuple(round(float(v), 4) for v in polygon.bounds)
    except Exception:  # noqa: BLE001 - a shape with no bounds is its own key
        return (float(id(polygon)),)


__all__ = [
    "MAX_ADAPTER_FETCHES",
    "MAX_JURISDICTIONS",
    "AdapterRegistry",
    "build_registry",
    "discover",
    "expand",
    "matches",
]
