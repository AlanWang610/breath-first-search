# 0051 — A fitted vector is the runner's when they save it, and the router has to agree first

Status: **accepted**, 2026-10-23. Amends ADR 0021; builds the check ADR 0020 deferred.

## Context

ADR 0021 shipped NEUTRAL until a fit cleared a bar, and said that at that point "the fitted
vector becomes the default for the runner it was fitted for - never globally". M23 amended
the bar: held out, the fit's 95% floor above NEUTRAL's agreement, with at least `MIN_PAIRS`
real pairs and a detour penalty of zero.

The first real fit cleared it on 2026-10-21: 50 pairs judged by one runner,
`lts3=0.25, lts4=0.5, unpaved=2, path_bonus=0.5`, held out 64.9% (95% 51.1%-76.7%) against
NEUTRAL's 37.7%. Nothing could use it. ADR 0021 had not said three things:

- **Where the vector lives, and who puts it there.** "Becomes the default" happens to nobody
  in particular.
- **What draws with it.** `cli/plan.py`, `api/app.py` and the MCP `route` tool each chose
  between NEUTRAL and AVOID_HIGH_STRESS in their own line, and the MCP tool sent no model
  at all. The plan sheet was meant to name the vector and never did.
- **Whether the router agrees with the costing.** ADR 0020 scores a vector by costing lines
  that exist and deferred "a small validation pass that does re-route". M23 then found the
  case costing cannot see: every vector that sought anything was a request GraphHopper
  refused, and the arithmetic was fine throughout.

## Decision

**The vector lives in the runner's profile.** `PreferenceProfile.routing` holds the six
multipliers and a record of the fit behind them (`FittedRouting`: labeler, pairs, held-out
numbers, re-route counts). NEUTRAL with provenance `default` ships in `defaults.yaml`. A
saved fit is `inferred`, as a pacing curve from history is: derived from what the runner
did, so anything they state outranks it (scope 6.3).

**The runner saves it, with `longrun tune --labeler NAME --save`.** Passing every gate does
not adopt a fit; running the command does. An agent does not run it (`CLAUDE.md`), for the
reason it does not run `label publish`: it decides what the person's routes look like.

**Six gates, cheapest first, all required** (`core/labels/adopt.py`):

1. `--labeler` names one runner.
2. At least `MIN_PAIRS` real pairs.
3. The best vector is not NEUTRAL. The profile already says neutral.
4. The fit's detour penalty is exactly zero.
5. Held out, the fit's 95% floor clears NEUTRAL (ADR 0021 as amended).
6. **The router agrees** (`core/labels/reroute.py`). Each pair's ends, from its task in the
   ledger, routed once with the fitted vector on that task's region graph - one call per
   pair, ADR 0020's deferred pass. The line that comes back is compared with the two the
   runner was shown, using the pairs' own rule for "the same route" (more than 85% of it
   within 25 m of a candidate). It lands on the chosen line, the rejected one, or neither.
   The check passes when the router refused nothing, the chosen side outnumbers the
   rejected side, and no re-drawn line is beyond the runner's detour tolerance.

Gate 6 runs only when 1-5 pass, so a fit that fails a cheap gate spends no routing calls.
A router that is not running stops the command ("start the router"). Counting it as a
refusal would blame the fit for a stopped container. A pair whose task is not in this
ledger (an edit pair, or answers from another machine) is counted and skipped. If none can
be re-drawn, nothing is saved. `tune --reroute` runs gate 6 on its own and saves nothing.

**One function chooses the vector for every surface.** `custom_model.drawn_with(profile,
neutral=, avoid_high_stress=)`: a flag names a vector, otherwise the profile's is used, and
both flags at once are refused. `longrun plan --neutral`, `PlanSubmission.neutral` and
`neutral=` on the MCP `route` and `alternatives` tools draw with stock foot_priority over a
saved fit, for one plan, without editing anything.

**The sheet says which vector drew the route.** `Manifest.drawn_with` keeps the vector and a
sentence about it. For example: "fitted for alan on 2026-10-23 from 50 pairs: lts3=0.25, ...;
held out 65% against neutral's 38%". Or, after `--neutral`: "neutral, as asked (stock
foot_priority); your fitted vector (...) was not used". The manifest is persisted with the
scratchpad, so a resumed plan reports what its first half was drawn with.

**The profile file stays the runner's.** `save_profile` dumps the whole model, which would
drop a hand-kept file's comments and write out every default as though it had been stated.
`store.save_entry` replaces the one `routing:` block, or appends it with a comment, and
copies every other line byte for byte.

## Consequences

- **A neutral vector still sends no model**, so every recorded request body and cassette key
  is unchanged. The suite now points `LONGRUN_PROFILE_PATH` at a file no test has, because a
  saved fit in `~/.longrun/profile.yaml` would otherwise reach every test that plans a route.
- **The bar is NEUTRAL, not AVOID_HIGH_STRESS.** The first fit does not clear
  AVOID_HIGH_STRESS (55.8% held out, inside the fit's interval). It does not have to:
  AVOID_HIGH_STRESS is a flag a runner asks for, and the question ADR 0021 asked is whether
  a fit should replace NEUTRAL as what they get by default.
- **"Neither" does not fail the check.** A third line is a route nobody judged, and ADR 0020
  says such a route is not evidence. It is counted, so a vector that mostly draws lines
  nobody was shown is visible in the report.
- **Gates do not measure evidence per parameter.** `tune` prints the metres of difference each
  parameter has seen; the first fit's `lts4` rests on 7.9 km. The grid's tie-break pulls an
  unevidenced parameter to 1.0, so a non-neutral value has some evidence behind it, though
  possibly not much. Reading that line is the runner's part of the decision.
- **Saving again replaces the block, and deleting it goes back to neutral.**

**What would change this.** A saved vector whose re-drawn lines are mostly "neither" is
drawing routes nobody has judged. The next step is to seed pairs with the fitted vector as
one of `SEED_VECTORS`, so its own lines are put in front of the runner, rather than to
loosen the check.
