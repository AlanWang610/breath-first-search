# 0037 — The vision spot-check is not built, and the ceiling is the reason

Status: **accepted**, M14, 2026-09-23.

## Context

Scope §8.1 step 8 is *"Optional `imagery_tile` spot-checks on remaining ambiguous segments"*,
and §7.9 caps it at about ten calls per plan. Every plan this project has produced records:

```
step 8 skipped: no vision call site looks at imagery
```

(`agent/loop.py:364`). That note has been honest since M10 and its blocker has changed once
already — it used to say "no imagery provider is configured", which stopped being true when
ADR 0023 configured one. `imagery_tile` now fetches a real tile, metered, cached and
attributed. What has never existed is anything that *looks* at one.

This is not blocked on effort. `fetch_tile` returns bytes and a media type, the loop holds a
`Cache` and a `Budget`, and pydantic-ai takes a multimodal payload. It is four or five
ordinary pieces of work, and that is exactly why it needs a decision rather than a backlog
entry.

## Decision

**Not built.** Four arguments, in the order they bind.

**1. ADR 0015 already ruled on this shape.** Its closing sentence is the rule: *"a call site
with no deterministic answer is not a call site, it is a dependency, and adding one is a
decision to revisit this."* Every one of the five existing call sites degrades to a
deterministic path — a terse comparison string, a templated question, an unchanged profile,
a structured request the CLI already built. A vision spot-check has no such path. There is no
non-model answer to "what is this segment actually made of", which is the whole reason the
step wants a model. So it is a dependency, and ADR 0015 says adding one revisits that ADR
rather than extending it.

**2. The two budgets do not compose, and the failure is silent.** `imagery_tiles_max = 10`
meters tiles; `model_calls_max = 12` meters model calls and is itemised in its own comment as
*"one intent parse, up to five trade-off comparisons across five rounds, three elicitation
questions, a preference proposal, and slack"* (`core/models/context.py:75-79`). Ten vision
calls do not fit in that, and the way they fail to fit is the problem: `ask` charges
`budget.spend_model_call()` and returns **`None`** on `BudgetExceeded` rather than raising
(`agent/model.py:103-105`). So a vision step that ran early and spent the slack would not
error. It would quietly starve the trade-off and elicitation sites later in the same plan,
and the runner would get worse prose with nothing anywhere saying why. Any real design needs
a separate meter or a raised ceiling, and ADR 0015 derived that twelve on purpose.

**3. It widens the one function the project narrowed deliberately.** `ask(agent, prompt: str,
*, budget)` is typed for text, and `model.py`'s docstring is that there is *one place where a
model is built, and one place where a call is charged for*. A multimodal payload widens
exactly that function. Workable, but it is a change to the project's only charging point in
service of its weakest call site, which is the wrong order.

**4. The resolution ceiling is the argument that would still bind if the other three were
solved.** USGS imagery stops at zoom 16 — measured against the live service, not read from
its metadata, which advertises 23 levels (`core/data/tiles.py:19`). At z16 a pixel is about
**1.9 m** on the ground at this latitude: *"enough to tell a trail from a road"*
(`tiles.py:26`) and not enough to tell a sidewalk from a shoulder. The segments a plan is
genuinely unsure about are mostly sidewalk questions — `surface_profile` lowers confidence
where OSM does not tag a sidewalk, and scope §12 already records that *"sidewalk-vs-roadway
position cannot be resolved from GPS traces or heatmaps"*. A spot-check that cannot answer
the question it is called for is a call that costs a tile, a model call and a plan's latency
to return "unclear".

## Consequences

**Step 8 keeps saying it was skipped, and keeps naming the blocker.** The note is pinned
verbatim in a golden expectation, so changing it is a deliberate act and not a drift.

**There is nothing to select even if the model existed.** §8.1 says *"remaining ambiguous
segments"* and nothing in the tree classifies a segment that way. The only `ambiguous` in
`core/` is the cue sheet's turn-ambiguity flag, which is about a navigation instruction
rather than about what a segment is made of. A selector is net-new work and the harder half:
picking the ten segments worth a tile is the part that decides whether the feature is worth
anything, and it is unwritten.

**`imagery_tile` stays useful as a tool.** A person can call it, and the MCP tool returns
base64 bytes for a client that can render them. What this ADR refuses is the *automatic* leg
inside the planning loop, not the capability.

**One inconsistency is left standing and named.** `tools/io.py:98` builds a throwaway
`Budget()` per call, so the ten-tile cap is per-call there rather than per-plan. That is
documented at the call site and is the correct behaviour for a tool invoked by hand; it would
have to change the day the loop called it.

## Revisit when

- **A basemap with better than ~0.5 m resolution is configured.** `LONGRUN_TILE_PROVIDER=custom`
  already admits one. At that point argument 4 dissolves and only the budget question remains,
  which is a number rather than a design.
- **A deterministic classifier gives step 8 something to select.** If `surface_profile` ever
  emits a ranked "uncertain" list, the selector exists and the spot-check becomes a way to
  resolve a named shortlist rather than a trawl.
- **A second scorer wants imagery**, which would make a shared vision seam worth its cost
  where one caller never was.

Until then the honest state is a step that is skipped and says so, which is scope §3.6
applied to the loop's own behaviour rather than to its data.
