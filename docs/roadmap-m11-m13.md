# Roadmap: M11–M13

Status: **executed and superseded**, 2026-09-23. M11 (#8), M12 (#10) and M13 (#9) are all on
`main`. Kept as a record of what was planned against what was built, not as a live plan.

## What executing it corrected

A plan is worth committing partly for the things it got wrong. The milestones shipped
fourteen corrections to this document; these are the ones that changed a design rather than a
number.

- **M11.3's rule was necessary and not sufficient.** This document said "re-key by distance,
  not index". Matching on `cum_start_m`/`length_m` alone still re-points silently, because an
  equal-length replacement mid-route leaves every downstream distance unchanged. The
  implementation keeps the span as the index and compares the *endpoints on the ground*.
- **The first tier-4 bug was misdiagnosed here.** This document blamed `polygon=None` making
  `runs_along` return `inf`. The records never reached `closures` at all — `fetch` collected
  features only from the adapter loop, and extraction happens outside it. Both mechanisms were
  real; fixing either alone would have left the symptom unchanged.
- **"Both matrix legs" was impossible.** Windows runners cannot host Docker service
  containers, so the contract tier is a separate ubuntu-only job (ADR 0034).
- **"Show it as missing, not as zero" understated the bug.** The timeline was not drawing
  zero; it filtered nulls and drew one polyline through the rest — a straight line across the
  gap, which reads as flat ground.
- **Two tests were misfiled by marker, not seven**, and cost was never why they were excluded.
- **The predicted `sheet_md.py` merge conflict did not exist.** M11 never touches that file.
  What was there instead was worse: M11.6 made one of its docstrings false, and because no
  branch edited the file, it merged clean and silently stale.

Both features this document declined are now written down: ADR 0035 (tier-4 search) and
ADR 0037 (the vision spot-check). The evidence gap it declined to touch is still open, by
design — see "Two things this plan deliberately does not do" below.

## Why this document exists

PR #6 refers to "the M8–M14 sketch". That sketch is not in the repository, not in git
history, and not in any branch — it lived in a conversation and evaporated with it. The
single forward reference that survived is "M11.7" in the FCC commit, which is not enough to
reconstruct from. This file is the replacement, and it is committed for exactly that reason.

M0–M10 have landed. Against scope §7 the tool surface is complete: 41 tools, 21 of 21
scorers, all 10 `gpx_verify` checks, all 12 of §9's plan-sheet contents. What is left divides
into three piles, and only two of them can be worked autonomously.

**Not in these milestones, by decision:**

- **The evidence gap.** Zero eval labels are `by: human` and zero preference pairs are real,
  so the shipped custom model stays neutral (ADR 0021) and R1 stays unanswered. ADR 0022
  exists to stop an agent manufacturing either. `tests/eval/` is not touched by M11–M13.
- **The vision spot-check** (§8.1 step 8). See "Two features deliberately not built" below.
- **Tier-4 search** (§7.10's crawler). Same section.
- **External errands**: the FCC BDC download (manual, its CDN 403s every non-browser
  request), HPMS (no reachable source, ADR 0012), and the GraphHopper LTS import (needs a
  JDK and Maven on the host).

---

## M11 — An edit becomes something a plan can survive

**Thesis.** Everything §10.3's direct manipulation needs that is not a browser. `ui/README.md`
claims "what is missing is the endpoints and the map interactions, not the capability." That
is true for lock and mostly true for avoid polygons. It is false for the rest, and this
milestone is the difference.

The blocker is segment identity. `segment_id(index)` is `f"s{index:05d}"`
([segments.py:34-36](../src/longrun/core/geo/segments.py#L34-L36)) — purely positional.
`run_scorers` merges carried results **by scorer name only**
([registry.py:208](../src/longrun/core/scorers/registry.py#L208)), so a carried
`ScorerResult` keeps `measurements[].segment_id` from the old segmentation. Any edit that
moves the line renumbers everything downstream of it, and every carried measurement then
points at different ground — silently. `rescore_plan` sidesteps this by never re-routing and
says so (ADR 0030); §10.3's "each action re-runs only the affected scorers" has no such
escape.

**M10.12 already solved this shape of problem** — turn instructions located by distance
rather than by index. M11 applies the same move to scorer measurements.

| # | Work | Files |
|---|---|---|
| M11.1 | `LockedRange` learns who wrote it (`source: "user" \| "loop"`). Today a user lock and the loop's `reason="round 3: rerouted"` / `"chose B"` are indistinguishable, so "unlock what I locked" has no discriminator. | `core/models/request.py:45-62`, `agent/loop.py:224,608` |
| M11.2 | `Scratchpad.unlock(start_m, end_m)`. `lock()` appends with no merge, no dedupe, no removal. | `core/plan/scratchpad.py:69-80` |
| M11.3 | **Measurements re-key by distance, not index.** A carried `ScorerResult` maps onto the new segmentation through `cum_start_m`/`length_m`; a measurement whose span no longer exists is dropped and *reported*, never silently re-pointed. This is the milestone. | `core/scorers/registry.py`, `core/geo/segments.py` |
| M11.4 | `pin_waypoint` gets an owner. It is `unavailable(blocked_on="work")` today with the reason "a via point is a property of the request, and editing a stored request in place has no owner yet" ([editing.py:115-122](../src/longrun/tools/editing.py#L115-L122)). Give stored-request editing that owner. | `tools/editing.py`, `core/plan/scratchpad.py` |
| M11.5 | `Route.source = "edited"` is actually written. The literal is declared at [geometry.py:50](../src/longrun/core/models/geometry.py#L50) and set by nothing in `src/`. | `core/geo/gpx.py`, edit paths |
| M11.6 | `start_window` wired end-to-end. It has two references in the tree — its own declaration and its own validator. No CLI sets it and `start_time_optimizer` ignores it. Either wire it to the sweep or delete it; do not leave a third state. | `core/models/request.py:78`, `core/scorers/start_time_optimizer.py`, `cli/plan.py` |
| M11.7 | Partial re-score across a geometry edit, on M11.3. Must keep `run_scorers(only=None)` bit-exact — the golden suite is what checks that ([registry.py:193-194](../src/longrun/core/scorers/registry.py#L193-L194)). | `core/plan/refresh.py`, `core/plan/pipeline.py` |
| M11.8 | A CLI path for every edit action, before any UI control exists (scope 3.9). This is also how M11 is tested without a browser. | `cli/` |

**Verification.** Full local gate (`ruff check`, `ruff format --check`, `mypy`, `pytest -m
"not network and not slow"`). The load-bearing assertion is that **no golden expectation
moves**: M11 changes no measurement, and `measurements_sha256` is keyed on `segment_id`
([expectation.py:115-133](../tests/golden/expectation.py#L115-L133)), so a moved hash means
M11.3 renumbered something it should not have. Add a test that edits a route, re-scores
partially, and asserts the carried measurements land on the same ground as a full re-score —
that comparison is the whole point and nothing checks it today.

---

## M12 — The UI becomes the editable version

**Thesis.** §10.3, now that M11 gave it a foundation. Every control calls a capability that
already worked from the CLI in M11.

**Note before starting:** `ui/` has **no automated verification of any kind**. `package.json`
has `typecheck` (`tsc --noEmit`) and no test runner, and `.github/workflows/ci.yml` never
enters the directory. M12.8 is not optional polish; it is the only thing that will catch a
regression in this milestone's own work.

| # | Work | Files |
|---|---|---|
| M12.1 | An id→path resolver for writes. `_plan_path` ([app.py:499-510](../src/longrun/api/app.py#L499-L510)) is the only traversal guard in the codebase and it guards a *read*. The `tools/` layer is file-path-parameterised throughout; a POST that took a scratchpad or GPX path would reopen that hole with write semantics. Every write endpoint takes an id. | `api/app.py` |
| M12.2 | Write endpoints: lock, unlock, choose-an-alternative, add-a-via, avoid-polygon. Each is a `jobs.submit(work)` — `submit` is generic over `Callable[[Reporter], Scratchpad]` ([runner.py:120-142](../src/longrun/jobs/runner.py#L120-L142)) and needs no runner change. `resume` cannot be reused: it is hard-wired to `agent.loop.answer` and raises unless a question is pending. | `api/app.py` |
| M12.3 | `PlanSubmission` reaches parity with `PlanRequest` for `avoid_polygons` and `avoid_names` — and the CLI grows the same flags in the same commit. Its docstring is the rule: "a parameter that existed here and not on the CLI would be a capability only the UI had" ([app.py:53-58](../src/longrun/api/app.py#L53-L58)). | `api/app.py`, `cli/plan.py` |
| M12.4 | MapView handler lifecycle. Handlers are registered once inside a first-draw `else` branch with no `instance.off()` anywhere ([MapView.tsx:120-144](../ui/src/components/MapView.tsx#L120-L144)), so they close over the first plan; and `fitBounds(..., {duration: 0})` refits on every plan change ([:147-149](../ui/src/components/MapView.tsx#L147-L149)), which will throw away the user's pan on the first write that round-trips. Both must be fixed before any gesture works. | `ui/src/components/MapView.tsx` |
| M12.5 | Click a flagged segment → choose an alternative (auto-locks). Segment click→`(start_m, end_m)` is already derivable client-side from `cum_start_m + length_m`, which keeps capability out of the UI. Note there is **no UI path from a stored plan** today — choosing only exists on a live parked job. | `ui/src/components/MapView.tsx`, `App.tsx` |
| M12.6 | Draw an avoid polygon. The polygon must be rounded by `detour.round_coordinates` at `AREA_PRECISION` and size-checked against `MAX_AREA_KM2` **server-side** — unrounded coordinates poison the routing cache key, and M5.13 was the milestone spent finding that out ([avoid.py:22-25](../src/longrun/core/routing/avoid.py#L22-L25)). An over-cap area is refused by name with its size, never silently dropped. | `api/app.py`, `ui/` |
| M12.7 | Timeline scrubbing highlights the map position. `Timeline.tsx` is a pure function of `plan` with no handlers, and no distance/cursor state is shared with `MapView`. | `ui/src/components/Timeline.tsx` |
| M12.8 | Vitest for the pure UI functions (`flagsBySegment`, `tierColour`, segment→range math), plus `npm run typecheck` and the new tests wired into CI. | `ui/package.json`, `.github/workflows/ci.yml` |

**One invariant this milestone must not break.** `RoutingPolicy` is frozen and resolved once,
persisted specifically so a resume in another process cannot compute a different one
([routing.py:14-19](../src/longrun/core/models/routing.py#L14-L19)). Adding an avoid polygon
mid-plan contradicts that. The honest option is a re-route, not a patch to a policy the first
half of the line was already drawn without — say so in the UI rather than pretending.

**Deferred with a reason: the chat pane.** It wants the MCP session and the browser talking
to one agent. `api/` reaches `tools/` only by importing the same `core/` functions those
tools wrap, never through `MCPServer` itself. That is a second integration, not a panel.

**Verification.** Gate plus `npm run typecheck` and `npm test`. Then drive it: `uv run
longrun api --ui ui/dist` and exercise each control against a stored plan, checking that the
resulting `plan.json` carries the edit. A headless agent cannot verify a MapLibre drag
gesture — say so in the PR rather than implying coverage that does not exist.

---

## M13 — The thin-data region, and the tier that never ran

**Thesis.** The test net, and four honest-reporting bugs in a path that is currently inert.

| # | Work | Notes |
|---|---|---|
| M13.1 | **Ozarks golden route** — scope §11's test region 3. Everything is on disk: PostGIS is loaded (osm, osm_lts, nhd, padus, tiger), the graph is built (`data/graphhopper/ozarks-lts-gh`, 9.1 MB, serves on port 8997), and the region is 1,600 ways, so fixtures will be the cheapest after `synthetic-hazards`. Headroom is 15.5 MB of the 64 MB cap. | See the recording constraint below |
| M13.2 | CI gets a `postgis/postgis:17-3.5` service container with the init SQL. That runs **43 of the 88** deselected tests (`test_layer_store_equivalence`, `test_freeze_fixture`, `test_postgis_schema`, `test_national_load`, and `test_osm_load` once the `ingest` extra is synced). | `.github/workflows/ci.yml` |
| M13.3 | Seven tests are misfiled by marker and free today: `test_cli.py:270` is marked `network` but only runs a subprocess, and `test_raycast_budget.py`'s five are `slow` CPU-budget assertions. Re-mark or enable deliberately. | |
| M13.4 | The FCC BDC loader, written against a fixture. The blocker is an errand, not a gate: no key is needed, but the CDN 403s every non-browser request, so the download is manual into `data/`. Write the loader and document the errand; `cell_coverage` stops reporting ABSENT the moment the file lands. | `core/data/`, `regions/build.py` |
| M13.5 | **Four latent tier-4 bugs**, each of which bites the moment an extractor is wired. `ModelExtractor` is dead code — zero instantiations, zero tests — so none of these is visible today. | See below |
| M13.6 | Stale references: `test_golden_routes.py:54` cites **ADR 0032**, which does not exist (it is 0031); `model.py:53` says "the **four** LLM call sites" and there are five; `prompts.VERSION` is read by nothing despite its docstring claiming it reaches the plan; `ci.yml:22-28` and `test_golden_routes.py:62-65` both cite the 26m2s Windows job from before the golden suite got 221 s faster. And `README.md:10` still says **"Status: scaffold. Directories and package boundaries only — no implementation yet."** | |

**The four tier-4 bugs (M13.5).**

1. `registry._extracted` passes `polygon=None`, so a tier-4 feature gets an empty
   `GeometryCollection`, `runs_along` returns `inf`, and `closures.py:254` drops it — while
   still incrementing `JurisdictionAnswer.count`. The sheet would say "tier 4 answered, 3
   records" and show zero flags.
2. Tier-4 escapes `MAX_ADAPTER_FETCHES`: `_extracted` neither checks nor increments the
   counter, contradicting `context.py:76-77`. A route crossing 40 uncovered jurisdictions
   makes up to 40 model calls against a 12-call ceiling; calls 13+ return `None` and read as
   "extraction returned nothing".
3. Promotion is broken at the provenance link. `draft_adapter` requires a `source_url` and
   nothing in the codebase produces one — `ExtractedClosure` has no URL field.
4. `JurisdictionAnswer.confidence` is never set by `_extracted`, so "a manifest entry marked
   unverified" (§7.10) is conveyed by `tier=4` alone.

**Recording constraint for M13.1 — read before starting.** A cassette is permanent and
unrepeatable. WZDx feeds have no archive; weather is recoverable from Open-Meteo's archive
endpoint but air quality only forecasts about 7 days against weather's ~16. **The chosen date
must land inside both windows**, or the route records a half-scoring cassette — which is what
the first `kc-stateline` attempt did. Do the recording in one sitting, with the network up and
GraphHopper serving on 8997.

**Set expectations honestly in the PR.** Scope §11 predicted region 3 would have "likely no
WZDx". It has **9 of 9 jurisdictions covered by a tier-1 MoDOT adapter**, so this route
exercises the adapter path for closures, not the honest-absence path. The absences it
genuinely demonstrates are buildings, `cell_coverage`, and railways/transit at 0 rows. That is
a finding, and `deploy/regions/ozarks.yaml` already records it — do not quietly let the PR
imply §11's prediction held.

**Verification.** Gate, plus `pytest -m golden` with the new route, plus the CI run proving
the Postgres service works on both matrix legs. Watch the Windows job's wall clock: it ran
26m2s against a 30-minute timeout after M9, and although the golden suite is now 221 s
faster, M13.2 adds 43 tests and a service container. M6's rule stands — *"the day this is hit
again the answer is to look at what got slow, not to raise it again."*

---

## Two features deliberately not built

Both are in scope. Neither is blocked by effort, and that is why each needs a decision rather
than a backlog entry. **Write an ADR for each** rather than leaving them unmentioned.

**The vision spot-check (§8.1 step 8).** Four things make this a decision:

- ADR 0015's closing rule is directly on point: *"a call site with no deterministic answer is
  not a call site, it is a dependency, and adding one is a decision to revisit this."* A
  vision spot-check has no deterministic fallback.
- The budgets do not compose. `imagery_tiles_max = 10` meters tiles; `model_calls_max = 12`
  is itemised in a comment as intent + five trade-offs + three questions + a proposal +
  slack. Ten vision calls do not fit, and because `ask` returns `None` on `BudgetExceeded`
  rather than raising, an over-budget vision step would **silently starve the trade-off and
  question sites later in the same plan** — no error, just worse prose.
- `ask(agent, prompt: str)` is the project's single charging point and is typed for text. A
  multimodal payload widens the one function whose docstring is "one place where a model is
  built, and one place where a call is charged for."
- The resolution ceiling is the honest objection. USGS imagery stops at zoom 16 ≈ 1.9 m/pixel
  — enough for trail-versus-road, not for a sidewalk. Most segments that are *ambiguous* in
  `surface_profile` terms are sidewalk questions. There is also no "ambiguous segment"
  concept in code: nothing classifies a segment that way and nothing maps one to a
  representative lat/lon.

**Tier-4 search (§7.10).** `ExtractionRequest` carries no URL and no text. Search means a
discovery step, a fetch step through `cache.fetch` with a new tool name and args hash, an
HTML/PDF-to-text step, and a new field on a frozen dataclass the registry constructs — plus
cassettes, which ADR 0006's redistribution rule constrains. Fix the four correctness bugs in
M13.5 first; a search that feeds a path which silently drops what it finds is worse than no
search.

---

## How to run these

One branch per milestone, in order — M12 depends on M11.3, M13 is independent of both.
Sub-commits in the house style (`M11.1: <a prose title>`), a PR body shaped like #5 and #6,
and **stop at the open PR**. Nothing merges unreviewed.

Two sub-commits per milestone should be a *correction* commit if the work turns one up. M10
shipped seven corrections to the sketch it was executing and was better for saying so.

**Services this needs, all present locally and none in CI:** PostGIS
(`docker compose -f deploy/docker-compose.yml up -d`, currently up with all five regions
loaded), GraphHopper per region (`./deploy/graphhopper/run.ps1 -Region <name>`, graphs already
built for all five), Node 24 for `ui/`, and JDK 21 if a graph ever needs re-importing — Maven
is **not** on PATH, though the wrapper jar is already built.

**The gate, every time:**

```
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
uv run pytest -m "not network and not slow"
```

Baseline at the time of writing: **1,643 passed, 7 skipped, 88 deselected in 105 s.**
