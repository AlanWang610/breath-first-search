# 0042 — A tide conflict is soft, and the first soft flag in the safety tier

Status: **accepted**, M16, 2026-09-23.

## Context

Scope §7.6 signs `access_hours` as *"park gate and dawn-to-dusk violations at ETA; **tide
conflicts for beach segments**"*. M4 built the first clause and
[ADR 0013](0013-closures-may-hard-fail.md) settled its tier: soft, COMFORT, severity up to
0.9. M16 builds the second, and the tempting move is to inherit that row of ADR 0013's table
because it is the same scorer, the same ETA loop and the same `_summarise`.

**That inheritance would be wrong, and it would be wrong for the one reason ADR 0013 gave.**
Its argument is not "a gate is mild". It is:

> An arbitration tier is not a severity ranking; it is a claim of *incommensurability*, and
> SAFETY means no amount of everything below buys it back. **A gate is bought back by
> starting twenty minutes later.**

Everything after that sentence — §6.4 filing "earliest start (e.g. gate opens)" as a request
constraint, §8.1 step 7's `start_time_optimizer` being the machinery, the scorer surfacing
`earliest_feasible_start_shift_min` instead of a reroute demand — is downstream of it. So the
question for a tide is not "how bad is it" but **"what buys it back"**.

**Three things say the answer is "nothing the clock can do".**

*The sweep cannot reach it on an out-and-back.* A semidiurnal tide is ~12 h 25 min periodic
and the safe window is a fraction of a cycle. An out-and-back crosses the same stretch twice,
separated by however long the run takes. If those two crossings straddle high water there is
**no** start time that clears both, because shifting the start shifts both crossings together.
`start_time_optimizer` sweeps a window of hours; it cannot manufacture a second low water.

*The failure is not a detour.* You stand at a locked gate, you turn round, you are
inconvenienced. A tidal causeway that floods behind you removes the way back, and the ground
you are standing on is the hazard. That is what "no amount of everything below buys it back"
describes.

*The instruction to the runner is a different verb.* A gate is solved by **starting earlier**.
A high water is solved by **waiting** — at the near edge of the stretch, for the water to fall.
Those are different numbers with different meanings, which is why this ships
`tide_wait_to_low_water_min` rather than folding the tide into the existing start shift.

The counter-pressure is equally real. `hostility`, `legality`, `crossings` and `closures` all
reach `Tier.SAFETY` **only** as `FlagKind.HARD`, so nothing in the codebase yet produces a
soft safety flag; `arbitrate` supports the combination and only the tests exercise it. And
the evidence is an OSM tag, which for a beach is an inference.

## Decision

**A tide conflict is `FlagKind.SOFT` at `Tier.SAFETY` when the evidence is `tidal=yes`, and
`FlagKind.SOFT` at `Tier.COMFORT` when the evidence is `natural=beach`.**

| code | evidence | kind | tier | severity |
|---|---|---|---|---|
| `tide_conflict_at_eta` | `tidal=yes` | SOFT | **SAFETY** | 0.95 × 0.9 |
| `tide_conflict_at_eta` | `natural=beach` | SOFT | COMFORT | 0.95 × 0.5 |
| `tide_unknown` | `tidal=yes` | SOFT | **SAFETY** | 0.35 × 0.9 |
| `tide_unknown` | `natural=beach` | SOFT | COMFORT | 0.35 × 0.5 |

**Soft, not hard, and ADR 0013 already decided that half.** Its rule is that *a scorer emits
HARD flags if and only if §7.9 gives it a check*, operationally because
`VerifyReport.offending_segments()` feeds §8.1 step 9 back into rerouting and a finding with
no route back to rerouting has no business being hard. §7.9 fixes ten checks, `access_hours`
owns none of them, and ADR 0013 closed the door on an eleventh. A hard tide flag would be
decoration with one live consequence: `_is_close` treats a difference in hard count as never
close, so it would silently become undiscussable in arbitration.

**The tier splits on the evidence, not on the consequence, and ADR 0013 is again the
precedent.** Its gate 5 uses adapter confidence to decide hard-versus-soft and to scale
severity. Here the same two channels are used one rung down: the *class* of evidence decides
the tier, and the confidence attached to that class multiplies the severity. `tidal=yes` is a
categorical statement by whoever mapped it that the water covers this way — ADR 0013's own
test for a datum a scorer may flag without inventing a sign. `natural=beach` says the ground
is beach and leaves the water inferred, so it may not claim incommensurability.

**`tide_unknown` keeps its stretch's tier rather than dropping to COMFORT.** The gate scorer
drops severity when it cannot establish an arrival (`UNKNOWN_TIME_SEVERITY`) and keeps the
tier, and the same reasoning is stronger here: filing "this way floods and nobody could tell
us when" under comfort would say that not knowing about a tidal causeway is a comfort matter.
The severity is what falls, to 0.35, because the *claim* is weaker — not the subject.

**The station seam is `STATION_REACH_M = 50 km`, not `GATE_REACH_M = 200 m`, and it is not
the feature seam at all.** `_place` drops any feature further than 200 m from the route, which
is right for a gate — a gate is a point on the ground the runner arrives at. A tide station is
a *reference gauge* tens of kilometres away, and what it governs is a **stretch**, not a point.
So the tide half does not use `FeatureSource`, `_place` or `GATE_REACH_M`. It reads the `ways`
layer, builds contiguous runs with `_coastal.coastal_stretches`, and asks CO-OPS for the
station nearest each run's midpoint; a station beyond 50 km produces a `checked=False` row
naming the distance, which is a different answer from "the endpoint failed".

**A route with no tidal stretch reports nothing at all.** No flag, no measurement key, no
coverage row — `tidal_stretches` is *absent*, not zero. That is the distinction
`tests/golden/expectation.py` already draws for waypoint kinds ("no key" and "zero" should
read differently), and once a stretch does exist all three counters are written, zeroes
included, so that `tide_conflicts: 0` beside `tide_stretches_unchecked: 0` is a legible
"measured as none". The fourth answer has its own row: a route whose `ways` layer could not be
read records `checked=False` against `ways`, because on that route nobody established whether
the tide matters and silence would be indistinguishable from an inland route.

## Consequences

* **`arbitrate` gains a real soft safety flag.** A tide conflict enters `TierScore.weighted_sum`
  for SAFETY, so it outranks any amount of physiological and comfort improvement
  lexicographically while leaving `hard_count` at zero. That is exactly the instrument the
  incommensurability claim needs and it changes no verification outcome: `_offenders_from`
  filters on `FlagKind.HARD`.
* **No golden moved, and none could.** None of the seven routes crosses tidal ground; measured
  rather than assumed, none of their `ways` fixtures carries `tidal`, `natural`, `ford` or a
  sand/mud/shingle surface at all. The classifier is therefore covered by unit tests alone.
* **`access_hours` now reads the `ways` layer.** It did not before; it read `parks` through
  `route_jurisdictions` and adapters through `ctx.features`. One more corridor query per plan,
  the same one `legality` and `surface_profile` already each make.
* **The scorer's two halves are independent.** `_gates` was extracted whole because its four
  early returns each called `_summarise`, so a second question asked after any of them would
  have been skipped on exactly the routes that took them — a route with no parks layer is
  precisely a route that could still be on a beach.
* **`start_time_optimizer` sees a second time-dependent output from this scorer.** It already
  re-runs `access_hours` across the sweep, so a tide conflict moves with the start like
  everything else; what it must not do is read `tide_wait_to_low_water_min` as a start shift.

## What would make us revisit

**A golden route that actually crosses tidal ground.** Everything above is reasoning plus unit
tests. A coastal golden — a Boston harbour-walk variant, or an Ocean Beach out-and-back — would
be the first end-to-end evidence that the classifier fires on a real extract and that a
SAFETY-tier soft flag behaves in arbitration the way this document claims. It is the single
highest-value follow-up and it was not built here because recording one needs a region build
and a committed CO-OPS cassette.

**The first false positive.** `tidal=yes` appears on ways that are tidal for ten minutes a
month as well as on the Broomway. If a real route produces a SAFETY flag a runner would
shrug at, the fix is a height rule — `TideExtreme.height_m` is already carried and unused
for exactly this — not a retreat to COMFORT.

**`HIGH_WATER_WINDOW_MIN = 90`.** It is the least evidenced number in the milestone: it
treats every tidal way as flooding at the same point in the cycle, which is what having no
path elevation in the tide's datum costs. VDatum would remove the guess and add a service.

**A soft SAFETY flag turning out to be undiscussable in practice.** `_is_close` compares
weighted sums within a tier, so two candidates differing only by a tide conflict *can* be
offered as a trade-off. If that reads badly to users — "would you like the route that
floods?" — the answer is a rule in `arbitrate`, not a tier change.
