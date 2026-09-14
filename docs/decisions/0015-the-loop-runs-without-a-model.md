# 0015 — The loop runs without a model

Status: accepted (2026-09-12)

## Context

Scope §4.1 names five fixed points at which an LLM is called — parse intent, choose among
same-tier alternatives, write trade-off explanations, draft tier-4 extractions, propose
preference updates — and is emphatic that it "is not handed the whole tool bag to sequence
freely". What the scope does not say is what happens when there is no model at all.

That question is not hypothetical, and three things in the repository already answer it in
the negative if nobody decides otherwise:

* `tests/golden/harness.py` opens with "Through the CLI, with **no model in the loop** and
  nothing reaching the network." Every golden route is run that way and `tests/README.md`
  repeats it. A loop that needed a model could not be golden-tested at all.
* `conftest._block_network` fails any non-`network` test that opens an off-host socket, so
  a model call inside the hermetic suite is already an error — just an obscure one.
* The scope defines no model, no provider, no token budget and no cost cap anywhere in its
  385 lines. A design that made the loop depend on one would be inventing a requirement.

There is also a plain engineering reason. Scope §4.2 argues that the control flow "fits in
~150 lines" and needs no state-graph library. If the model decides *what happens next*,
that argument collapses: the flow is no longer known in advance, and the ~150 lines become
a graph after all.

## Decision

**`agent/loop.py` is deterministic control flow. Every model call site is optional, and
each has a deterministic path when it is absent.**

`CallSites` carries four callables, each defaulting to `None`, and `NO_MODEL` is the
instance every test and every golden route runs with:

| Call site | Absent behaviour |
|---|---|
| intent parsing | a structured `PlanRequest` is required; a sentence raises `IntentUnavailable`, naming the extra and the key |
| the trade-off one-liner | `compare`'s existing fallback, `f"{a.label} and {b.label} score within {margin}"` |
| a preference proposal | nothing is proposed and the profile is unchanged |
| elicitation phrasing | a template naming the axis, the options and the mile |

Tier-4 extraction is the fifth site and is not on `CallSites`: it already reaches the
registry as an `Extractor`, and M4 built `NullExtractor` for exactly this reason.

`None` marks an absent site rather than a Null object. That follows
`ScorerContext.features`, where M4 chose `None` over a default instance because "no
registry was configured" and "a registry, but nobody covers this county" are different
claims and the sheet has to be able to make both. The same distinction applies here: "no
model was configured" and "a model that had nothing to say" are not the same, and a Null
object that reproduced `compare`'s fallback string would be a second copy of it.

## Consequences

**A golden route exercises the whole of §8.1 — route, pace, score, arbitrate, reroute,
re-score, verify — with no model in it.** The suite stays hermetic and reproducible, and
the model becomes an enrichment at four points rather than a dependency of the flow.

**The plan says what it spent.** `Budget.spend_model_call()` and
`Manifest.model_calls_used` / `.model_tokens_used` exist because a plan that cannot say
what it asked a model for is the §3.6 failure in a new currency. The scope has no token
budget to transcribe, so `model_calls_max = 12` is a decision: one intent parse, up to five
trade-off comparisons across five rounds, three elicitation questions, a preference
proposal, and slack. Tokens are recorded and not capped — refusing a long answer halfway
through is a worse failure than an expensive one.

**The hermetic suite is stopped at the model layer, not only at the socket.**
`pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` in an autouse fixture makes "a test
called a model" a loud, specific failure, which is what `_block_network` does for sockets.
`ANTHROPIC_API_KEY` does not start with `LONGRUN_`, so `_clear_longrun_env` does not clear
it and a developer's real key would otherwise be reachable from a test that passes in CI.

**The risk this leaves open** is a *new* call site added later without a deterministic
path, which would make the claim quietly false. That is why the shape is an ADR and not a
convention: the rule is that a call site with no deterministic answer is not a call site,
it is a dependency, and adding one is a decision to revisit this.

## What this does not decide

Whether a model is *good* at any of these four jobs. That is M6's evaluation work, and
nothing here assumes an answer — `tests/eval/` exists and is empty for that reason.
