# 0033 — The start window reaches the sweep on the context, not through the scorer contract

Status: **accepted**, M11, 2026-09-22.

## Context

`PlanRequest.start_window` has existed since M1 and had **two references in the whole
tree**: its own declaration and its own validator. No CLI set it, nothing on the API set it,
and `start_time_optimizer` — the one scorer whose entire subject is *when to leave* — ignored
it and said so in its module docstring:

> **The window is relative to the requested start, not supplied.** A scorer receives
> `(route, segments, ctx, etas)` and never the `PlanRequest`, so it cannot read §6.4's start
> window. … Passing a real window is the agent's job (§8.1) when it exists.

Scope §6.1 offers "date and intended start time, **or a start-time window**", and §8.1 step 7
says "if a start-time window was given, `start_time_optimizer` before the final round". The
agent exists now, and the field is still inert. A field the scope asks for, that a milestone
has not wired and has not deleted, is the third state the roadmap is right to refuse: it
reads as a capability to everyone who greps for it and is a no-op to everyone who uses it.

So it is wired, and the question is *how a scorer learns it*.

## Decision

**On `ScorerContext`, as `start_window: TimeWindow | None`.**

Three routes were available and two of them are worse.

**Widening the scorer contract** to `(route, segments, ctx, etas, request)` would hand all
twenty-one scorers the whole request so that one of them could read one field. That is the
contract `core/scorers/base.py` exists to keep narrow — *"one context type, not per-scorer
keyword arguments … what lets scorers be written in any order by anyone and tested against
nothing but synthetic geometry"* — and a scorer that can see `PlanRequest` can see
`avoid_polygons`, `locked` and `overrides`, none of which it has any business measuring
against.

**Running the sweep outside the registry**, as §8.1 step 7 reads on a first pass, means the
scorer runs twice or leaves the registry — and then it is not in `SCORERS`, not in
`freshness`, not in the coverage drain and not in a refresh. Step 7 is about *when the answer
is used*, not about who calls the function.

**The context already carries exactly this kind of fact, and the precedent is
`utc_offset_hours`**: a property of the request that one group of scorers needs, that they
cannot read from the request, and whose absence is a real answer rather than a default. The
window is the same shape, so it goes the same way, and the CLI, the refresh and the API all
pass it from the request they already hold — `open_context(start_window=request.start_window)`
— rather than from a second parameter that could disagree with the request.

**`None` is not a whole-day window.** "I have not fixed an hour" and "any hour between
midnight and midnight" are different requests. With `None` the sweep keeps its own ±3 h span
around the requested start, which answers *"would earlier be better, and by how much"*. With
a window it sweeps that window end to end and **never reaches outside it**, which answers
*"when, within the hours I can actually leave"*. A recommendation to start at 05:30 is worth
nothing to somebody who said they cannot leave before seven.

**The summary says which was swept, in two shapes rather than one.** A plan with no window
carries `window_hours` and nothing else; a plan with one carries `window_from`,
`window_earliest` and `window_latest` and no `window_hours`. A sheet printing
`window_hours: 3.0` beside a 09:00–11:00 request would be describing a sweep nobody ran.

The keys are conditional because they had to be, and that was found by failing rather than
by reasoning. The first version of this added all three unconditionally, `null` on a plan
with no window — and `expectation.measurements_hash` walks *every key of every measurement*,
so the golden suite failed **all six routes** on a milestone that measured nothing new. The
rule `expectation._summary` already states for waypoint kinds is the one that applies:
*"kinds with no finds are omitted rather than written as zero — 'no key' and 'zero' should
read differently"*.

## Consequences

**`longrun plan --start-window HH:MM-HH:MM` drops the start time, and says so.**
`PlanRequest` has refused a start time and a window together since M1, so keeping the
default 07:00 alongside a window would be a validation error rather than a nicety. The plan
is paced and scored at the window's **earliest** — a time the runner actually named, rather
than a midpoint nobody did — and the command echoes that choice, because a start hour picked
silently is one the reader will assume they chose.

**The sweep costs more and it was measured rather than capped.** On a 2,000-point route
(≈100 km at 50 m spacing), with the ray-caster stubbed so the number is the sweep's own:

| window | candidates | seconds |
|---|---|---|
| default, ±3 h | 13 | 0.26 |
| 05:00–09:00 | 9 | 0.18 |
| 05:00–21:00 | 33 | 0.61 |
| 00:00–23:30 | 48 | 1.01 |

Linear in candidates at about 21 ms each, so the widest window a day can hold adds **0.75 s**
to scope §6.4's ~180 s budget. No cap, and no coarser step inside a wide window: the
half-hour resolution argument is about what makes an answer *actionable* — "an hour earlier"
is advice, "somewhere between dawn and noon" is not — and that does not change with the
width of the window. If this is ever the expensive part of a plan, the measurement above is
the thing to re-take rather than a number to guess at.

**`latest` is always a candidate**, even when it does not land on a half-hour step. It is a
time the runner named, and a sweep that quietly stopped twenty minutes short of it would be
answering a question nobody asked.

**A refresh keeps the window.** `longrun refresh` passes `stored.request.start_window`, so
re-scoring a plan against a new date does not quietly answer a different question from the
one `longrun plan` answered about the same route.

**No golden moves**, because no golden route sets a window and `None` leaves the old path
bit-for-bit: `candidate_starts(start)` with no `window` is the function it was.

## What this does not decide

**Whether anything should *choose* a start time.** `_best` reports the coolest and shadiest
candidates and ranks nothing, because scope §3.2 keeps the sign out of a scorer and
"coolest" and "shadiest" routinely disagree. §8.1 step 7's "before the final round" implies
the loop does something with the answer, and today it does not — the table reaches the sheet
and the runner decides. Making the loop *re-plan* at a chosen hour would change the ETAs
every other scorer was measured against, which is a whole pass and not an adjustment, and
nobody has asked for it yet.
