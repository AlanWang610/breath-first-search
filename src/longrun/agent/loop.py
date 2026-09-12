"""The scope 8.1 planning loop, as explicit code (scope 4.2).

Ten steps, a five-round cap, and no graph-workflow framework - scope 4.2 rejects one on
the grounds that the control flow is known in advance and a human-in-the-loop pause can be
handled by persisting state and returning `needs_input`. This module is that claim being
tested: `Scratchpad` has existed since M1, complete and round-trip tested, with **no caller
anywhere in the tree** until now.

**The loop runs without a model** (ADR 0015). Every one of the fixed call sites scope 4.1
names is optional, and every one has a deterministic path when it is absent:

* intent parsing - the request arrives structured, which is what both CLI entry points
  already build. A sentence needs a model; a `PlanRequest` does not.
* the trade-off one-liner - `compare` already falls back to a terse deterministic string.
* a preference proposal - proposes nothing, and the profile is unchanged.
* elicitation phrasing - a template naming the axis, the options and the mile.

So a golden route runs the whole of scope 8.1 with no model in it, which is what
`tests/golden/harness.py` promises in its first sentence, and the model is an enrichment
at four points rather than a dependency of the control flow. `None` marks an absent call
site rather than a Null object, for the reason `ScorerContext.features` gives: "no model
configured" and "a model that had nothing to say" are different claims and both have to
reach the sheet.

**The user chooses among same-tier alternatives, not the model** (ADR 0019). Scope 4.1
lists "choose among same-tier alternatives" as a call site; scope 8.1 step 6 says such
conflicts are surfaced to the user, and scope 8.4 gives the model the one-line comparison
and the runner the decision. `compare` has implemented the latter since M1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from longrun.core.geo.segments import locked_segment_ids
from longrun.core.models.plan import PendingQuestion, Plan, TradeOff
from longrun.core.plan.arbitrate import arbitrate, rank_flags, score_candidate
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.plan.scratchpad import Scratchpad

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date, datetime

    from longrun.core.models.context import Budget, ScorerContext
    from longrun.core.models.geometry import Route, Segment
    from longrun.core.models.plan import Manifest, SnapshotPins
    from longrun.core.models.profile import PreferenceProfile
    from longrun.core.models.request import PlanRequest
    from longrun.core.plan.arbitrate import Candidate
    from longrun.core.plan.pipeline import ScoredRoute
    from longrun.core.routing.base import Router

#: Scope 8.1 step 6: "iterate up to 5 rounds or until no scorer flags above threshold".
MAX_ROUNDS = 5

#: How many candidates to ask the router for per flagged span. A detour request answers
#: once - GraphHopper will not combine its alternatives algorithm with via points - so
#: this only bites on the whole-route fallback.
CANDIDATES_PER_ROUND = 3


class IntentUnavailable(RuntimeError):
    """A natural-language request arrived and nothing can read it.

    Named rather than generic, because the fix is specific and belongs in the message: a
    structured request works, or install the `agent` extra and set a key.
    """


@runtime_checkable
class IntentParser(Protocol):
    """Scope 8.1 step 1: a sentence becomes constraints."""

    def parse(self, text: str, *, today: date) -> PlanRequest: ...


@runtime_checkable
class Describer(Protocol):
    """Scope 8.4's one-liner: what differs between two same-tier candidates."""

    def __call__(self, a: Candidate, b: Candidate) -> str: ...


@runtime_checkable
class UpdateProposer(Protocol):
    """Scope 6.3: a conversational statement becomes a proposed profile entry."""

    def propose(self, statement: str, profile: PreferenceProfile) -> dict[str, Any] | None: ...


@runtime_checkable
class QuestionWriter(Protocol):
    """Scope 6.3's in-context question, in words a runner would use."""

    def write(self, axis: str, options: list[str], *, at_km: float) -> str: ...


@dataclass(frozen=True)
class CallSites:
    """The fixed points at which a model may be called (scope 4.1).

    Never the whole tool bag: the loop decides what to ask and when, and a call site that
    is absent has a deterministic answer rather than a missing one.
    """

    intent: IntentParser | None = None
    describe: Describer | None = None
    propose_update: UpdateProposer | None = None
    write_question: QuestionWriter | None = None


#: What every golden route and every test runs with.
NO_MODEL = CallSites()


def call_sites_from_env(budget: Budget | None = None) -> CallSites:
    """Whatever the environment can support, which is often nothing.

    Returns `NO_MODEL` when no key is configured, rather than raising: ADR 0015 makes an
    absent model a degradation and not an error, and a CLI that refused to plan without one
    would contradict every golden route in the suite.
    """
    from longrun.agent.model import settings_from_env

    if not settings_from_env().available:
        return NO_MODEL

    from longrun.agent.tradeoffs import describer

    return CallSites(describe=describer(budget))


@dataclass(frozen=True)
class LoopOutcome:
    """What one run of the loop produced.

    The scratchpad is the resume unit (scope 4.2) and is always present. `plan` is what a
    *finished* scratchpad renders into, so it is `None` exactly when the loop stopped to
    ask - which is the difference between a plan with no answer yet and a plan that failed.
    """

    scratchpad: Scratchpad
    plan: Plan | None = None
    scored: ScoredRoute | None = None

    @property
    def needs_input(self) -> bool:
        return self.scratchpad.status == "needs_input"


def plan_route(
    request: PlanRequest,
    ctx: ScorerContext,
    *,
    start_at: datetime,
    route: Route | None = None,
    router: Router | None = None,
    sites: CallSites = NO_MODEL,
    profile: PreferenceProfile | None = None,
    snapshot: SnapshotPins | None = None,
    resume: Scratchpad | None = None,
    max_rounds: int = MAX_ROUNDS,
) -> LoopOutcome:
    """Run scope 8.1 from a structured request to a plan, or to a question.

    Steps 1 and 2 are the caller's: a `PlanRequest` is the output of step 1, and step 2's
    history ingest lands in M5.10 - until then the pacing model reports the population
    curve and says so, which is the honest degradation and already implemented.
    """
    pad = resume if resume is not None else _fresh(request, profile, snapshot)
    if resume is not None:
        _apply_answer(pad)

    if pad.route is None:
        pad.route = route if route is not None else _route(request, router, pad)
    if pad.route is None:
        pad.status = "failed"
        return LoopOutcome(scratchpad=pad)

    manifest = pad.manifest
    _note_imagery(manifest)

    scored = _score(pad, request, ctx, start_at=start_at, router=router, manifest=manifest)

    while pad.round < max_rounds:
        flagged = _worst_segment(pad)
        if flagged is None:
            break
        span = (flagged.cum_start_m, flagged.cum_start_m + flagged.length_m)

        candidates = _propose(pad, router, span)
        if not candidates:
            # Scope 8.1 step 6 degrades to flag-but-don't-fix rather than looping on an
            # answer that will not change. R12: a detour around one segment in a dense
            # grid may be identical to the original or may not exist, and that is a
            # property of the city rather than of this code.
            break

        outcome = _arbitrate(
            pad,
            request,
            ctx,
            start_at=start_at,
            router=router,
            manifest=manifest,
            scored=scored,
            candidates=candidates,
            segment_id=flagged.id,
            sites=sites,
        )
        if outcome is None:
            return LoopOutcome(scratchpad=pad)
        winner, scored = outcome
        if winner is None:
            break

        pad.round += 1
        pad.lock(*span, reason=f"round {pad.round}: rerouted")

    pad.status = "complete"
    plan = _finish(pad, request, scored, manifest)
    return LoopOutcome(scratchpad=pad, plan=plan, scored=scored)


def answer(pad: Scratchpad, choice: str) -> Scratchpad:
    """Record the user's answer to whatever the loop stopped to ask.

    Separate from `plan_route` because a resume crosses a process boundary: something
    loads the scratchpad, writes the answer, saves it, and only then does a loop pick it
    up. A method that did both would make the two halves inseparable, which is exactly
    what scope 4.2's design is trying to avoid.
    """
    if pad.question is None:
        raise ValueError("nothing was asked")
    pad.answers[pad.question.id] = choice
    return pad


# --- the steps ---------------------------------------------------------------


def _fresh(
    request: PlanRequest, profile: PreferenceProfile | None, snapshot: SnapshotPins | None
) -> Scratchpad:
    from longrun.core.models.plan import Manifest
    from longrun.core.models.profile import PreferenceProfile as Profile

    plan_id = uuid.uuid4().hex[:12]
    return Scratchpad(
        plan_id=plan_id,
        job_id=uuid.uuid4().hex[:12],
        request=request,
        profile=profile if profile is not None else Profile(),
        locked=list(request.locked),
        manifest=Manifest(snapshot=snapshot) if snapshot is not None else Manifest(),
    )


def _route(request: PlanRequest, router: Router | None, pad: Scratchpad) -> Route | None:
    """Scope 8.1 step 3, and only in generate mode - repair mode skips to step 4."""
    if router is None or request.start is None or request.end is None:
        return None
    from longrun.core.routing.base import NoRouteError, RouterUnavailable

    waypoints = [request.start, *request.via, request.end]
    try:
        return router.route(waypoints)
    except (RouterUnavailable, NoRouteError) as exc:
        pad.manifest.degradation.append(f"no route: {exc}")
        return None


def _note_imagery(manifest: Manifest) -> None:
    """Scope 8.1 step 8, reported rather than silently skipped.

    `imagery_tile` has a budget meter and no implementation and no provider. A step that
    is not run and not mentioned is indistinguishable from a step that found nothing,
    which is scope 3.6's whole subject.
    """
    note = "step 8 skipped: no imagery provider is configured"
    if note not in manifest.degradation:
        manifest.degradation.append(note)


def _score(
    pad: Scratchpad,
    request: PlanRequest,
    ctx: ScorerContext,
    *,
    start_at: datetime,
    router: Router | None,
    manifest: Manifest,
) -> ScoredRoute:
    """Steps 4, 5 and 9, which `score_once` already runs as one pass."""
    assert pad.route is not None
    scored = score_once(
        pad.route, request, ctx, start_at=start_at, router=router, manifest=manifest
    )
    pad.route = scored.route
    pad.segments = scored.segments
    pad.etas = scored.etas
    pad.coverage = scored.coverage
    for result in scored.results:
        pad.put_result(result)
    return scored


def _worst_segment(pad: Scratchpad) -> Segment | None:
    """Step 6's "collect worst segments, excluding locked ranges".

    Ranked lexicographically by tier, then hard before soft - `rank_flags` already does
    that, and doing it any other way here would let a comfort flag outrank a safety one
    at the only point where the loop decides what to fix.

    A flag whose segment id no segment owns is a route-wide finding, which is a real
    thing several scorers emit; there is nothing to reroute around, so the loop looks
    past it rather than stopping.
    """
    excluded = locked_segment_ids(pad.segments, pad.locked)
    by_id = {segment.id: segment for segment in pad.segments}

    for ranked in rank_flags(pad.results, pad.segments, _total(pad), exclude=excluded):
        segment = by_id.get(ranked.flag.segment_id)
        if segment is not None:
            return segment
    return None


def _propose(pad: Scratchpad, router: Router | None, span: tuple[float, float]) -> list[Route]:
    if router is None or pad.route is None:
        return []
    return router.alternatives(pad.route, span, k=CANDIDATES_PER_ROUND)


def _arbitrate(
    pad: Scratchpad,
    request: PlanRequest,
    ctx: ScorerContext,
    *,
    start_at: datetime,
    router: Router | None,
    manifest: Manifest,
    scored: ScoredRoute,
    candidates: list[Route],
    segment_id: str,
    sites: CallSites,
) -> tuple[str | None, ScoredRoute] | None:
    """Score the field, choose, or stop and ask.

    Returns `None` when the loop must park - which is the one control-flow branch scope
    4.2's whole design exists for.
    """
    total = _total(pad)
    field: list[Candidate] = [
        score_candidate("original", pad.results, pad.segments, total),
    ]
    scoredby: dict[str, ScoredRoute] = {}
    for index, candidate in enumerate(candidates):
        label = f"alternative {index + 1}"
        pass_result = score_once(
            candidate, request, ctx, start_at=start_at, router=router, manifest=manifest
        )
        scoredby[label] = pass_result
        field.append(
            score_candidate(
                label, pass_result.results, pass_result.segments, pass_result.route.length_m
            )
        )

    # `describe=None` is the deterministic path, and `compare` already owns the fallback
    # string - so an absent call site needs no Null object reproducing it (ADR 0015).
    decision = arbitrate(field, segment_id=segment_id, describe=sites.describe)

    if decision.needs_input and decision.trade_off is not None:
        pad.trade_offs.append(decision.trade_off)
        pad.question = _ask(pad, decision.trade_off, field, sites, segment_id)
        pad.status = "needs_input"
        return None

    if decision.winner is None or decision.winner == "original":
        return None, scored

    # Strictly better, or not at all. `arbitrate` ranks by `tier_key` and breaks ties on
    # the label, and "alternative 1" sorts before "original" - so a candidate that scores
    # *identically* would win, be adopted, and let the loop report a round of improvement
    # it did not make. That is the failure M3 and M4 both paid for, and a router that hands
    # back the original route is not a hypothetical: `alternatives` between the same two
    # endpoints returns the primary path first, which is exactly that.
    original, chosen_candidate = field[0], next(c for c in field if c.label == decision.winner)
    if chosen_candidate.tier_key() >= original.tier_key():
        return None, scored

    chosen = scoredby[decision.winner]
    pad.route = chosen.route
    pad.segments = chosen.segments
    pad.etas = chosen.etas
    pad.coverage = chosen.coverage
    for result in chosen.results:
        pad.put_result(result)
    return decision.winner, chosen


def _ask(
    pad: Scratchpad,
    trade_off: TradeOff,
    field: list[Candidate],
    sites: CallSites,
    segment_id: str,
) -> PendingQuestion:
    """What to stop and ask: a preference, when scope 6.3 permits one; else the choice.

    A preference question is the better one when it is allowed, because its answer settles
    this plan *and* every plan afterwards - where a route choice settles one segment of
    one run. Scope 6.3's conditions are narrow by construction and most rounds will fall
    through to the trade-off.
    """
    from longrun.core.preferences.elicitation import question_for

    at_km = _distance_of(pad, segment_id) / 1000.0
    elicited = (
        question_for(field[0], field[1], pad.profile, asked=pad.questions_asked, at_km=at_km)
        if len(field) > 1
        else None
    )
    if elicited is not None:
        pad.questions_asked += 1
        return PendingQuestion(
            id=uuid.uuid4().hex[:8],
            kind="preference",
            prompt=_phrase(elicited, sites),
            options=elicited.options,
            axis=elicited.axis,
            segment_id=segment_id,
            trade_off=trade_off,
        )
    return PendingQuestion(
        id=uuid.uuid4().hex[:8],
        kind="trade_off",
        prompt=trade_off.comparison,
        options=[trade_off.option_a, trade_off.option_b],
        trade_off=trade_off,
        segment_id=trade_off.segment_id,
    )


def _phrase(question: Any, sites: CallSites) -> str:
    """The words, from a model when there is one and a template when there is not."""
    from longrun.agent.questions import template, write

    if sites.write_question is None:
        return template(question.axis, question.options, at_km=question.at_km)
    return write(question.axis, question.options, at_km=question.at_km, detail=question.detail)


def _distance_of(pad: Scratchpad, segment_id: str) -> float:
    segment = next((s for s in pad.segments if s.id == segment_id), None)
    return segment.cum_start_m if segment is not None else 0.0


def _apply_answer(pad: Scratchpad) -> None:
    """A resolved trade-off auto-locks (scope 8.4).

    `Scratchpad.lock`'s docstring has said "called automatically when the user resolves a
    trade-off" since M1, and nothing in `src/` called it. A choice already made must not
    be silently reopened on the next iteration, which is what a loop with no memory of it
    would do.
    """
    question = pad.question
    if question is None:
        return
    choice = pad.answers.get(question.id)
    if choice is None:
        return

    # Both kinds of answer choose a route, so both auto-lock: scope 8.4 says the chosen
    # alternative locks, and it says nothing about *why* the runner was asked.
    #
    # What is deliberately not done here is writing the profile. Scope 6.3 stores an
    # elicited answer as `stated`, and the answer to "which of these two routes" is a
    # route - turning that into a *value* needs a direction table saying what choosing the
    # calmer route implies about `traffic_tolerance`, and inventing one here would file a
    # guess under the provenance that is meant to record that somebody said it.
    # `elicitation.record_answer` is the write path and it is tested; what it wants is a
    # value. M6 fits preference parameters against real pairs, and that is where the
    # direction table belongs.
    if question.segment_id:
        segment = next((s for s in pad.segments if s.id == question.segment_id), None)
        if segment is not None:
            pad.lock(
                segment.cum_start_m,
                segment.cum_start_m + segment.length_m,
                reason=f"chose {choice}",
            )
    pad.question = None
    pad.status = "running"
    pad.round += 1


def _finish(pad: Scratchpad, request: PlanRequest, scored: ScoredRoute, manifest: Manifest) -> Plan:
    plan = build_plan(
        scored,
        request,
        profile=pad.profile,
        coverage=pad.coverage,
        manifest=manifest,
        plan_id=pad.plan_id,
    )
    return plan.model_copy(
        update={
            "trade_offs": list(pad.trade_offs),
            "status": "needs_input" if pad.status == "needs_input" else "complete",
        }
    )


# --- small things ------------------------------------------------------------


def _total(pad: Scratchpad) -> float:
    return pad.route.length_m if pad.route is not None else 0.0


__all__ = [
    "CANDIDATES_PER_ROUND",
    "MAX_ROUNDS",
    "NO_MODEL",
    "CallSites",
    "Describer",
    "IntentParser",
    "IntentUnavailable",
    "LoopOutcome",
    "QuestionWriter",
    "UpdateProposer",
    "answer",
    "plan_route",
]
