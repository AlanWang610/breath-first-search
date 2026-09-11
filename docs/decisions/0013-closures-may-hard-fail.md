# 0013 — Closures may hard-fail a route; trail status and access hours may not

Status: accepted (2026-09-10)

## Context

M4 lands the three scorers scope §7.6 names and nothing has implemented: `closures`,
`trail_status`, `access_hours`. Each returns a `ScorerResult` that may carry `Flag`s, and
two accepted rules in this repository point in opposite directions about whether they may
carry any at all.

**[ADR 0006](0006-open-meteo-for-air-quality.md)** decided that `air_quality` produces *no*
flags, because §8.3's threshold table has no air-quality row and *"inventing one would be
attaching a sign to a measurement — which §3.2 forbids a scorer to do."* §8.3 likewise has
no row for closures, trail status or access hours. Read as a general rule, all three M4
scorers emit nothing.

**`check_6_no_active_closures`** reads only *hard* flags — `verify/runner.py::_offenders_from`
filters on `FlagKind.HARD`. Its docstring says it is *"Skipped whenever the closures scorer
had no adapter to consult — reporting a pass would claim the route was checked against
closure data that was never fetched."* If `closures` emits no hard flags, then the moment an
adapter *does* answer, check 6 passes **vacuously** on every route — which is worse than
skipping, because it reports a verified pass that verified nothing.

Both cannot hold.

**A correction that dissolves the conflict.** ADR 0011 asserts that hazards *"appear
nowhere"* in §8.3 and that §8.4's safety tier lists *"legality, hard crossings, LTS 4 — and
hazards are not there either"*. Both claims are false. §8.3 line 285 reads
`| Legality / hazards | — | any |`, and §8.4 reads *"safety (legality, hard hostility, hard
crossings, **hazards**, time constraints)"*. ADR 0011 was accepted on that basis. Its
decision survives on its other arguments — see the erratum appended to it — but the relevant
consequence here is that **"no §8.3 row ⇒ no flags" was never the operative rule of this
codebase**, and ADR 0006's sentence is a correct statement about air quality generalised
past its evidence.

Three golden routes currently pin all three scorers as `checked: false`. Whatever M4 writes
gets frozen, which is exactly the position ADR 0002 described: *"leaving the tier unsettled
means the first golden route records an accident."*

## Decision

**Two independent tests decide what a scorer may emit.**

*A scorer emits `HARD` flags if and only if §7.9 gives it a check.* `legality` → check 5.
`crossings` → check 8. `closures` → check 6. `hazards` has no check and emits no hard flags,
which is the consequence ADR 0011 already drew for itself. The converse is what this ADR
adds: check 6 exists, so `closures` must be able to fail it or check 6 is decoration. A hard
flag is operationally defined by `VerifyReport.offending_segments()` feeding §8.1 step 9
back into rerouting — a finding with no route back to rerouting has no business being hard.

*A scorer may emit `SOFT` flags when the datum is a categorical statement by whoever owns
the ground, or a threshold §6.3 supplies.* A WZDx `all-lanes-closed`, an NPS `Closure`
alert, a posted gate schedule: the sign is already in the datum and reporting it is not
§3.2's forbidden act. US AQI is neither — it is a continuous index nobody has given this
project a line for — so ADR 0006 remains right about its own subject.

| scorer | kind | tier | severity |
|---|---|---|---|
| `closures` | HARD behind five gates, else SOFT | SAFETY / COMFORT | 1.0 hard; base × confidence soft |
| `trail_status` | SOFT only | COMFORT | base × confidence |
| `access_hours` | SOFT only | COMFORT | up to 0.9 × confidence |

**A closure is hard only if all five gates pass.** Each exists because of a real property of
real data — the Maricopa County feed carries 115 work zones, most of them lane closures on
highways no pedestrian is on, one of them running from 2024-10-22 to 2028-06-16.

1. **Pedestrian passage is actually removed** — `vehicle_impact` of `all-lanes-closed`, or a
   category naming a sidewalk or full closure. `some-lanes-closed` is never hard.
2. **It lies along the route, not across it** — within `CLOSURE_BUFFER_M` (20 m) *and* within
   `PARALLEL_BEARING_DEG` (30°) of the route's bearing. You can get across a work zone; you
   cannot get through 400 m of closed sidewalk. This is the gate that stops every Phoenix
   route flagging.
3. **Active at the segment's ETA**, not merely on the route's date. §7.6 signs the tool
   `closures(gpx, date)` and the flag is tested at arrival.
4. **Not a standing condition** — a window longer than `STANDING_CONDITION_DAYS` (180) caps
   at soft. A four-year work zone is either stale or a permanent reconfiguration OSM has
   already absorbed, and either way it is not an event to reroute around.
5. **Tier ≤ 2 and confidence ≥ 0.8.**

**`MIN_HARD_FLAG_TIER = 2` is the load-bearing consequence.** A verification gate that can
be tripped by a model reading a PDF is worse than no gate. `MAX_EXTRACTION_CONFIDENCE = 0.5`
already makes tier 4 provably incapable of clearing gate 5, so half this rule is enforced by
a validator written in M1; this states the other half.

**Confidence gates kind and scales severity.** `arbitrate` never reads confidence — `Flag`
has no such field and adding one would re-open ADR 0002's invariant that one higher-tier
flag beats any lower-tier evidence. So confidence acts through the two channels that exist:
it decides hard-versus-soft (gate 5), and it multiplies soft severity, following
`hazards.CONFLATION_CONFIDENCE`.

**`access_hours` is soft at 0.9 despite a locked gate being genuinely disqualifying.** An
arbitration tier is not a severity ranking; it is a claim of *incommensurability*, and SAFETY
means no amount of everything below buys it back. A gate is bought back by starting twenty
minutes later. §6.4 already files *"earliest start (e.g. gate opens)"* as a **request
constraint**, and §8.1 step 7's `start_time_optimizer` is the mechanism. So the scorer
surfaces a proposed start shift as a route-summary measurement rather than a reroute demand —
and deliberately does **not** write it into `PlanRequest.time_constraints`, or check 9 would
begin failing on a constraint the user never stated.

**`trail_status` is soft because a park alert is prose.** "Muddy", "bridge out", "lion
sighted" — sorting the disqualifying from the merely wet *is* inventing a sign. The escape
hatch is structural rather than textual: §7.10 says a jurisdiction is a *"Census GEOID or
PAD-US unit ID"*, so an agency adapter that sees a structured closure emits
`Feature(kind="closures")`. One hard-flag owner, one check, no classifier.

**Check 6 becomes fail > skip > pass.**

```python
if closure_offenders is None:   return skip("no closure data available for this route")
if closure_offenders:           return fail(closure_offenders)
if unanswered:                  return skip(f"no closure adapter for {', '.join(unanswered)}")
return pass_()
```

A closure found in a jurisdiction that answered is a fact regardless of another
jurisdiction's silence, so `requires=` alone — which skips whenever any entry of the kind is
unchecked — would throw that fact away. Partial coverage skips; a positive finding fails.

**No eleventh check.** §7.9 fixes ten, and neither `trail_status` nor `access_hours`
produces hard flags, so a new check would have nothing to read.

## Consequences

* Check 6 can fail a route and send it back to rerouting. It is the second check that can,
  after check 5.
* **Tier-4 extraction provably cannot fail a route**, by arithmetic rather than by care.
* Check 6 will report `skipped` on most real routes for years, naming the jurisdictions with
  no adapter. That is §12 working as written, not a regression.
* `trail_status` joins `POSITION_WEIGHTED` at `MAX_WEIGHT`: a trail condition is something
  you endure and it compounds with fatigue, where a closed road is categorical. It was
  previously in neither collection and so behaved as never-weighted by accident.
* `CoverageEntry.__str__` and the golden coverage digest grow `jurisdiction` and `tier`,
  without which the suite cannot regress-detect the exact thing §7.6 asks a plan to report.
* ADR 0011 gains an erratum. Its decision stands; its account of §8.3 and §8.4 does not.

## What would make us revisit

**A `confidence` field on `Flag`, consumed by `arbitrate`,** if severity scaling proves too
blunt an instrument — a 0.55-confidence tier-3 closure and a 0.95-confidence tier-1 one
currently differ only in magnitude.

**A WZDx profile with a real pedestrian-impact field.** Gate 1 reads `vehicle_impact` and a
tag heuristic because pedestrians are not what the schema is about; a field that named them
would retire the heuristic outright.

**An eval set (M6) showing runners reject routes that gate 4's 180-day line waved through.**
The number is a judgement about what a work zone *is*, and it is the least evidenced thing
in this document.

**A trail-status feed structured enough to classify without prose.** That would let
`trail_status` produce hard flags on its own terms rather than deferring to an adapter
re-emitting `kind="closures"`.
