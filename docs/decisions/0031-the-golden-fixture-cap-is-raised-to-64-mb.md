# 0031 — The golden fixture cap is raised to 64 MB, and CI wall clock is the real limit

Status: **accepted**, M10, 2026-09-18.

## Context

The 50 MB cap has been in the build plan since M0 and was enforced by **nothing** — not a
test, not a CI step — until M9 made it `test_the_committed_fixtures_stay_under_the_cap`. What
stood in for it was M0's 15-minute CI timeout, on the stated reasoning that *"keeping CI short
is itself the enforcement mechanism for small fixtures"*. M6 raised that timeout to 30 minutes
when the fifth golden pushed the Windows job over, and recorded the reason in a comment in
`ci.yml` — a file away from anything about fixtures. So the only mechanism was relaxed by the
milestone that discovered it, and the cap was prose until M9.

By the time it became a test the suite was at **48.50 MB of 50**: 1.50 MB of headroom, which
is not enough for a seventh route. `boston-winter` alone is 8.0 MB, and it is already the
trimmed version — two layers of nine, because a 300 m corridor around 2.5 km of the Charles
holds 14,158 ways.

M9 wrote down the choice this forces: *"the next route to want space should force a decision
about the cap rather than shave this one — the honest options are trimming `bay-urban`
(14.4 MB, nine layers) or raising 50, and both are choices somebody should make on purpose."*

## Decision

Raise `MAX_FIXTURE_BYTES` to **64 MB**.

## Consequences

**M10 spends almost none of it, and that is worth stating.** Waypoints added about 2 kB across
six `expected.json` files, and the cue sheet records no fixture at all — its test data is a
7.5 kB JSON payload under `tests/unit/data/`, deliberately not under `tests/golden/routes/`.
So this is provision for M11–M13 rather than a need of the milestone that made it, and it was
taken as a decision rather than under pressure, which is the condition M9 asked for.

**64 rather than a round 75.** `boston-winter` at 8.0 MB is the model for a dense new route,
so 64 leaves room for about two more. Past that the CI job binds before the disk does, and a
cap set beyond the point where it stops being the binding constraint is not a cap.

**Disk is not what bites; CI wall clock is.** The Windows job ran **26m2s against a 30-minute
per-job timeout** on the main-branch run after M9 merged, and every golden test reads these
GeoPackages — six route-parametrized tests over six routes is 36 full pipeline runs. Raising
the byte cap does nothing about that. M6's rule for the timeout still stands and is the one
that matters next: *"the day this is hit again the answer is to look at what got slow, not to
raise it again."*

Two consequences follow from that being the real constraint, and both are honoured in M10:

* the GPX assertion was **folded into an existing parametrized test** rather than given its
  own, because a seventh route-parametrized test is another six pipeline runs;
* the cue sheet gets **no golden route**, and the reason is not bytes. `expectation.digest`
  cannot see a cue sheet, and if it grew a block for one it would pin street names from a
  dated OSM extract — so a rename would fail a golden and read as a code regression.
  `expectation.py` already refuses to pin trade-off prose for exactly this reason. The
  end-to-end cost is real and named in the milestone notes rather than hidden.

**What would change this decision:** a measurement of where the CI time actually goes. None
has been taken — the assumption that `boston-winter`'s 7.4 MB GeoPackage is the cause was
checked and is wrong (the golden suite went 209.11s → 212.92s when that route landed, about
four seconds). Until somebody runs `pytest --durations`, both this number and the 30-minute
timeout are being set without evidence, and the next milestone to arrive here should get the
evidence first.

---

## Amendment, 2026-09-22 — the measurement was taken, and it was not the fixtures

The ADR asked for `pytest --durations` before anyone touched the timeout again. Run on
2026-09-21, `pytest -m "not network and not slow" --durations=40`:

| | |
|---|---|
| whole hermetic suite | **306 s**, 1643 passed |
| golden tier | **272 s — 89% of it** |
| every one of the top 26 durations | a golden route test |

The cause is not bytes and not route count. Four tests each called `harness.run()` per
route, so six routes cost **24 full CLI pipeline runs** where six would do; the other three
assertions are read-only properties of the same frozen `GoldenRun`. (This ADR said 36 above,
from six parametrized tests — two of the six never run a pipeline, so the true figure was
24. The error did not change the conclusion, and it is corrected here rather than left.)

A session-scoped fixture that runs each route once:

| | before | after |
|---|---|---|
| golden tier | 272 s | **64 s** |
| whole suite | 306 s | **85 s** |
| outcome | 1643 passed, 7 skipped | unchanged |

**What this does to the decision.** The 64 MB cap stands — it was forward provision for
M11–M13 and still is. What does not stand is the reasoning: the byte cap was raised in the
belief that wall clock was the binding constraint and the fixtures were what spent it. Wall
clock *was* the binding constraint and the fixtures were not what spent it. The two
consequences listed above — folding the GPX assertion into an existing test, and giving the
cue sheet no golden — were both taken to avoid "another six pipeline runs". The first is now
worth about a second and would not need avoiding. **The second stands entirely on its own
merits**: its argument was never cost, it was that a digest pinning street names from a dated
extract turns an OSM rename into a false regression. That argument is untouched.

M6's rule — *"look at what got slow, not raise it again"* — was the right rule and it
returned the right answer the first time it was actually applied.
