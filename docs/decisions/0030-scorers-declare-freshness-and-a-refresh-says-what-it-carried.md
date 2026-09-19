# 0030 — Scorers declare freshness, and a refresh says what it carried

Status: **accepted**, M10, 2026-09-18.

## Context

Scope §7.8 asks for a refresh that re-scores "time-dependent" scorers against a new date and
reuses everything else. `longrun refresh plan.json` has been advertised in `cli/__init__.py`
since the first commit and did not exist; the `refresh_plan` MCP tool re-ran **all
twenty-one** scorers, returned a dict, and wrote nothing anywhere.

Two things had to be decided before it could be built: which scorers are time-dependent, and
how a plan says that seven of its results were not re-measured.

## Decision

**1. Freshness is two exhaustive frozensets in `core/scorers/freshness.py`**, in the shape
`core/plan/weighting.py` already established — and for the reason its own comment gives:
*"an unlisted scorer is indistinguishable from one someone decided about, and `trail_status`
sat in that state from M1 until M4 found it."* Seven time-independent, fourteen
time-dependent, with a test that fails on an unclassified scorer.

Not a per-scorer module attribute: reading the classification would then require importing
all twenty-one modules, so `--dry-run` would pay the full import cost to print a list, and a
module that failed to import would yield *no* classification rather than an error.

The set is **broader than scope §7.8's own parenthetical**, which names closures, weather, AQ
and trail status. `lighting` is solar elevation against civil twilight, `transit` is a
service-day question, `bailouts` reach a stop that may not be running, and
`start_time_optimizer` is anchored on the requested start. A refresh that carried those would
report last week's last train.

**2. A carried result says so on `ScorerResult.carried_from`** — the planned start of the
pass that measured it.

Not `ToolCall.cached`: that is written by `cache.fetch` and means "this external call was
served from SQLite". A carried scorer makes no external call, so marking one would mean
fabricating a `ToolCall` for work that did not happen, corrupting `total_elapsed_s` and the
call count at once.

Not a coverage entry, which was the obvious home and is checkably wrong:
`cli/export.py` already reads source-keyed `unchecked()` as a statement about a scorer, so a
carried marker there flips carried scorers to "unavailable" in `longrun summary` — a lie in
the opposite direction from the one being prevented. It also encodes a per-scorer fact in
`CoverageEntry.source`, which is *not reliably a scorer name*: `hazards` records `"dem"`,
`_score_pass` records `"way_matching"`, and several scorers record a layer constant.

A `datetime` rather than a `bool`, because `lighting` at 05:00 and at 19:00 are different
answers and a reader needs to know how stale rather than merely that it is.

**3. `only` must be closed under `PRIOR_DEPENDENCIES`, and `run_scorers` refuses rather than
expanding.** This is the one place a partial pass can produce a *wrong* number rather than a
stale one: carrying `sun_exposure` while re-running `heat_stress` against a new date feeds
WBGT a shaded fraction computed from last month's solar geometry, silently, in the
physiological tier. `closure()` is the public helper a caller with a terminal uses so the
widening is *reported*.

## Consequences

**The merge happens inside `run_scorers`, not in a caller**, and that is forced rather than
chosen. `prior` is assembled from the list under construction, so a carried result is only
visible to a later scorer if it already sits at its registry position; and the coverage drain
iterates `results`, so carried results seeded there reach the manifest with no new code.
Merging outside loses their coverage entries or drains in two places, and the second is a
double-count waiting for its first refactor. A test asserts a partial pass and a full one
produce a **byte-identical coverage block, in order**.

**A consequence of the closure rule, found by writing the test:** because a declared prior is
always re-run, it is never carried. The stale-prior case cannot arise rather than being
handled — so the seeding's real property is completeness and ordering, not staleness.

**`carry()` keeps `result.carried_from or as_of`.** A result carried through five refreshes
keeps the date it was *originally* measured for; overwriting it each time would make a
six-month-old `legality` report as one day old after a single refresh, turning the honesty
field into a laundering mechanism.

**A refresh reports over flags, not geometry.** It passes no router, so the line is identical
by construction, which makes `route_diff` on one *provably* vacuous — it returns "same line;
+0 m" every time, after comparing every point of one route against every point of the other.
Worse than wasteful: a reader who sees "same line" beside a refresh may conclude nothing
changed when the trail has just closed. `result_diff` is the score-delta half that
`core/plan/__init__.py` has described `diff.py` as having since M1 and that never existed.

**`scorers/base.py::publish` is deleted.** Dead since M1, and it did exactly what
`run_scorers`' drain does — under a partial pass its first caller would have doubled one
scorer's coverage entries on refreshed plans only, in the block `expectation.digest` compares
verbatim, with no existing test to catch it.

**Making the refresh write armed four latent bugs**, all harmless only because the tool had
never written its result: the stored manifest was reused (so `total_elapsed_s` accumulated
forever and scope §6.4's budget became unmeasurable), a new uuid plan id was minted, the three
fields `build_plan` does not write were dropped (discarding resolved trade-offs and flipping
`needs_input` to `complete`), and the context was opened with empty snapshot pins. The fifth
was the dangerous one: `_score_pass` writes `sample_elevation`'s answer onto the route, and
that is all-`None` off-DEM, so a refresh on a machine without rasters would silently destroy
the elevation profile it was refreshing. `score_once` gained `elevations=`, and its test
carries a **sabotage half** — the same call without the override must wipe the profile — so
the fix cannot outlive the reason for it.
