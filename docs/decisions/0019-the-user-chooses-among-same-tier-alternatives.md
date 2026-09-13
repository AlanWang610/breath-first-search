# 0019 — The user chooses among same-tier alternatives; the model writes the comparison

Status: accepted (2026-09-12)

## Context

Two accepted sentences in the scope disagree about who decides.

§4.1, listing the fixed LLM call sites:

> parse intent, **choose among same-tier alternatives**, write trade-off explanations,
> draft tier-4 extractions, propose preference updates

§8.1 step 6, describing the loop:

> Same-tier conflicts are surfaced to the user (persist scratchpad, `needs_input`); the
> chosen alternative auto-locks.

and §8.4:

> a one-line trade-off … **which is the LLM's job in this loop**. Residual flags are always
> listed.

So one sentence gives the model the choice and two give it the sentence. This is ADR 0013's
situation exactly — two accepted statements, one operative rule — and it has to be settled
before `agent/loop.py` is written, because the two readings produce different code: under
§4.1 the loop never parks and `needs_input` is dead, and under §8.1 it parks and the model
writes prose.

`core/plan/arbitrate.py` has implemented the second reading since M1. `compare` returns a
`TradeOff` rather than a winner when the decisive difference is inside one tier and below
`TRADEOFF_MARGIN`, and `Arbitration.needs_input` exists to say so.

## Decision

**The loop never resolves a same-tier conflict on the user's behalf. The model writes the
one-line comparison; the runner chooses.**

§4.1's "choose among same-tier alternatives" is superseded by §8.1 step 6 and §8.4, and
this ADR is the erratum. The call site survives with its job changed: it is
`agent/tradeoffs.py`, it is handed both candidates, and its output is `TradeOff.comparison`.

The reason is the same one the lexicographic ordering exists for. A tier boundary is a
claim of **incommensurability** — no weight a user sets on shade may outrank a hard
hostility flag, and §8.4 is structured so that such a trade is unrepresentable rather than
merely discouraged. Within a tier the ordering says the opposite: these things *are*
comparable, but by a margin too fine for the arithmetic to settle. A model picking across
that margin is not serving the arbitration scheme, it is overruling it, and it would be
doing so with less information than the runner has — who knows whether they mind a
sidewalk gap at mile 41 on a hot afternoon, and the model does not.

## Consequences

**`needs_input` is load-bearing rather than vestigial.** `Scratchpad.status`,
`PendingQuestion`, `Scratchpad.answers` and `agent.loop.answer()` exist because the loop
genuinely stops, and scope §4.2's bet — that a pause is a persisted scratchpad and not a
special control flow — is what gets tested.

**A resolved trade-off auto-locks.** `Scratchpad.lock`'s docstring has said "called
automatically when the user resolves a trade-off" since M1 and had no caller in `src/`
until M5.5. A choice already made must not be reopened on the next iteration.

**A candidate must be *strictly* better to be adopted.** Not a restatement — it is the
same principle one step down, and it is a real bug that this ADR's framing caught. When a
candidate ties the original, `arbitrate` ranks by `tier_key` and breaks ties on the label;
"alternative 1" sorts before "original", so an identical candidate would win, be adopted,
and let the loop report a round of improvement it did not make. A router handing back the
original route is not hypothetical: `alternatives` between the same two endpoints returns
the primary path first, and until M5.4 `cli/plan.py` scored it again as `alt-0`.

**Most plans will never ask.** `TRADEOFF_MARGIN = 0.15` and a decisive difference must
fall inside a single tier, which is narrow by construction. A milestone where trade-offs
fired often would have got §8.4 wrong.

## What this does not decide

Whether the model should ever choose *anything*. It still parses intent, drafts tier-4
extractions and proposes preference updates — and every one of those is confirmed,
capped or floored before it takes effect.
