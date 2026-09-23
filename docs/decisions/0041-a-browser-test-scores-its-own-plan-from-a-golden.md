# 0041 — A browser test scores its own plan from a golden, and commits nothing

Status: **accepted**, M15, 2026-09-23.

## Context

Every gesture in scope §10.3 acts on a **stored plan**. A click selects a stretch of one, a
lock writes into one, `GET /api/plans` lists them and `GET /api/plans/{id}` is what the page
opens. So a suite that drives those gestures needs a plan on disk before it can do anything
at all, and the repository has none — by an explicit rule, written in `.gitignore`:

> Plans written by `longrun plan --out` and served by `longrun api --plans`. **A stored plan
> is a run of somebody's, not a fixture: the goldens are where a plan this project tests
> against belongs, and one committed here would be a sixth golden nobody reviewed.**

That rule is about `plans/`, but its argument is not about a directory. A plan parked under
`ui/gestures/fixtures/` would be the same object under a different path: a full scored
`Plan` — route, segments, per-scorer results, coverage, metrics, profile — that no golden
test reads, that drifts from the schema the first time a scorer adds a key, and that arrived
in a UI milestone where nobody would look for a seventh route's worth of expectations.

Three options.

**Commit one.** Refused by the rule above, and the rule is right.

**Hand-write a minimal one.** Cheaper to read and the worst of the three, because a
hand-written plan is a second schema. It would encode somebody's belief about what the
server dumps — and M15's own corrections are two examples of that belief being wrong for
five milestones at a time. A suite built on a hand-written plan could not have caught
either.

**Score one at test time from a golden.** `tests/golden/harness.py` already does exactly
this, six times per run: `longrun repair <route.gpx> --date … --fixtures … --cache … --out
<tmp>` with `LONGRUN_OFFLINE=1`. It is deterministic, router-free, hermetic and proven.

## Decision

**The browser suite scores its own plan at test time, through the CLI, from a committed
golden route; it writes outside the repository; and each test takes a fresh copy.**

Five parts.

**1. The argv mirrors `tests/golden/harness.py`.** Same command, same pinned arguments read
from the route's own `request.yaml` — `--date`, `--start`, `--target-km`, `--utc-offset` —
and the same `LONGRUN_OFFLINE=1`. A change to how a golden is entered shows up here as a
failure rather than as a quiet divergence.

**2. `synthetic-hazards` is the route.** 143 KB against 5–15 MB for the others, its fixtures
were written rather than downloaded so there is no licence question in reading them twice,
and it scores in 8.5 s to 19 segments and 17 residual flags across seven scorers — enough
flagged ground that a hover has prose to show and a click has a tier colour to land on.

**3. A second route, `kc-stateline`, is scored for one test.** M12.4's camera guard is
`fittedFor.current !== plan.id`, and `plan.id` is derived from the route, so every copy of
one seed carries one id: to that guard, two stored copies of one plan are the same plan.
Only a genuinely different route can show that a different plan still refits, and without
one the guard could have been `fittedFor.current !== null` and passed everything. `seed.ts`
asserts the two ids differ, so the test cannot quietly stop testing anything. It costs nine
seconds.

**4. The cassette is copied, never read in place.** `LONGRUN_CACHE_DIR` names a directory
the server may open read-write, and a golden's `cache.sqlite` is a committed fixture six
routes reproduce against. The copy is what the server is pointed at, so no failure mode of
this suite can reach a golden's bytes. The `fixtures/` directory is read where it sits,
because nothing writes to it.

**5. Each test gets its own plan id, copied fresh.** Every gesture rewrites `plan.json` — a
lock writes a lock, a via writes a via, a `choose` rewrites the geometry. Tests sharing one
plan would pass or fail on the order they ran in, which is the failure the `A CORRECTION`
commit on `main` is about: *a test that agreed with whoever ran it last*.

## Consequences

**The suite spends about eighteen seconds scoring two plans on every run**, out of roughly
seventy end to end. That is the price of not committing a plan and it is worth paying, since
what it buys is a fixture that is regenerated from the source of truth on every run and can
never be stale.

**`choose` became testable, which was not expected.** `_choose_alternative` re-scores through
`ToolSettings.from_env()`, so it needs fixtures and a cassette or it reaches the network —
the reason M15 was scoped with `choose` possibly out. Because the seed already comes from a
golden, the server can simply be pointed at that golden's `fixtures/` and the copied
cassette with `LONGRUN_OFFLINE=1`, which is the arrangement the golden harness runs under
anyway. The spliced line re-scores against recorded data with no cache miss. All five
gestures are covered rather than four.

**The suite depends on the goldens staying enterable through `repair`.** If a golden route's
`request.yaml` grew a field the seed does not pass, the seed would score something slightly
different from what the golden tests score. `seed.ts` reads the pinned arguments per route
and checks the result has segments and distinct ids, which catches the loud cases and not
the quiet ones.

**Nothing is written inside the repository.** The seed, the server's `plans/` and the
server's job store all live under the system temp directory. `ui/test-results/` and
`ui/playwright-report/` are the only paths in the tree the suite can touch, and both are
gitignored, because both are a record of one run on one machine.

## What this does not decide

**Whether a golden route should ever be the UI's fixture formally.** Today the browser suite
reads `tests/golden/routes/` by path, which couples two tiers that had nothing to do with
each other. The coupling is one directory name and a list of arguments, and making it a
shared helper would put a UI concern inside the golden harness — worse, for now.

**What happens if the goldens are ever moved or trimmed.** `seed.ts` would fail loudly at
`globalSetup` with the route name in the message, which is the right failure, but nothing
tells somebody trimming a golden that a UI suite reads it. If that becomes a real risk the
answer is a note in `tests/golden/README`, not a mechanism.
