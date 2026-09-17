# Tuning and acceptance metrics

Scope §7.1 asks for two things this document is the answer to: the ~6 priority parameters
*"fit against pairwise route preferences"*, and acceptance metrics *"published for a set of
reference routes"*.

**The short version: the metrics are published below, the parameters are still not fitted for
want of human preference pairs — and as of M9 we know *why* the Bay Area measurements kept
coming back empty.** It was never that the parameters do nothing. It is that four of the five
built regions have a low-stress network so well connected that GraphHopper's stock
`foot_priority` already finds it, and the LTS term merely agrees. In the one region without
that luxury the same term is worth tripling a route's length.

Last updated 2026-09-16 (M9).

## The question R1 asked, and M9's answer

§7.1 treats the six parameters as useful. Three Bay Area measurements disagreed:

| When | What was compared | Result |
|---|---|---|
| M0.5 | `avoid` (`lts >= 3 → ×0.2`) vs `neutral`, four Bay Area pairs | detour ≤ **+0.8%**; `avoid ≈ neutral` on 3 of 4 |
| M5.4 | mild `lts>=3 ×0.2` on a detour request | route moved **0 m** |
| M5.4 | severe `lts>=2 ×0.02` on the same request | 8152 → 8095 m, so the mechanism *does* work |
| M6.2 | `--avoid-high-stress` vs neutral, Ferry Building → de Young | **identical**: 7694.7 m both |

M6 recorded a hypothesis: *"the likeliest explanation is not that the parameters are wrong but
that these pairs offer no choice… the way to test it is more pairs in more places."* M9 built
the other three regions' graphs and ran exactly that test, on real LTS rather than the
placeholder the graph had carried until then.

**`avoid` against `neutral`, 2026-09-16, all five regions, real LTS:**

| Region | Network | Detour | LTS ≥ 3 share, neutral → avoid |
|---|---|---|---|
| Bay Area | dense grid | +0.0%, +0.1%, +0.8%, +0.0% | 0–3% → 0% |
| Boston | dense grid | −0.1%, +0.0%, +0.2% | small → 0% |
| Kansas City | dense grid | +0.0%, +0.0%, +0.0% | small → 0% |
| Phoenix | arterial sprawl | +0.0%, +0.0% | small → 0% |
| **Ozarks** | **one highway** | **+50.1%, +207.5%, +104.4%** | **100% → 1.9%** |

The hypothesis was right, and the answer is sharper than "no evidence":

**The six §7.1 parameters are near-inert wherever a parallel low-stress street exists, and
decisive wherever none does.** That is a statement about *regions*, not about the parameters.
In Shannon County, Missouri, Highway 19 is the only road between Eminence and Round Spring:
the neutral route runs **100% at LTS 4**, and avoiding it costs three times the distance. In
San Francisco the same term changes nothing, because `foot_priority` has already done the job.

Two consequences worth stating:

- **This is not an argument for turning the default on.** ADR 0021 stands. The regions where
  the term is free are the regions where it is also pointless, and the region where it bites
  produces a 44 km answer to a 14 km question — which a runner might well refuse. Whether that
  trade is wanted is a *preference*, which is exactly what there is still no human data for.
- **It does explain why Bay Area pairs were never going to settle R1.** A preference set drawn
  from a dense grid cannot distinguish a good LTS weight from a useless one, because every
  candidate vector produces the same route. Pairs from thin-network regions carry far more
  signal per pair, and M14's labelling effort should be weighted accordingly.

`seek` — the control, `lts >= 1 && lts <= 2` — reaches 80–99% of length at LTS ≥ 3 in every
region, so the mechanism demonstrably steers everywhere. The small detours are the cities
answering, not the model failing.

*One correction M9 made to the instrument itself.* The `seek` control had been
`lts <= 2` since the spike, and the `lts` encoded value stores **0 for a way with no `lts`
tag** — so `seek` rewarded every untagged way while `avoid`'s `lts >= 3` ignored them. The two
were not mirror images. Every route above carries `lts0 = 0.0%`, so no measurement here is
affected, but earlier `seek` figures were flattered by an unknown amount.

## Acceptance metrics for the reference routes

§7.1's three: fraction of length at LTS ≥ 3, count of LTS 4 segments, detour ratio against
the shortest legal route. Measured 2026-09-13, from each route's published `expected.json`;
the detour ratio needs a live router and was measured against the Bay Area LTS graph.

| Route | Length | LTS ≥ 3 | LTS 4 | Detour ratio | Scored |
|---|---|---|---|---|---|
| `synthetic-hazards` | 3954 m | 20.0% | 4 | — | 100% |
| `bay-urban` | 2027 m | 5.7% | 2 | **0.990×** | 62% |
| `kc-stateline` | 3195 m | 1.8% | 1 | — | 76% |
| `phoenix-heat` | 5188 m | 0.0% | 0 | — | 100% |
| `loop-bayarea` | 7894 m | 0.0% | 0 | **1.043×** | 15% |

Three of these need reading rather than scanning.

**The two blanks are the two routes outside the Bay Area LTS graph.** Phoenix has its own
graph and Kansas City has none built — §13's graph step is a JVM step the region build
does not run. A blank here is "no router could answer", which is why it is blank and not 1.0.

**`loop-bayarea` is the loop's own trade, made legible.** The shortest legal route between
its endpoints is 7567.7 m; the loop adopted 7893.5 m. So §8.1 step 6 bought **4.3% of extra
distance and took the route's LTS ≥ 3 fraction to zero**. That is what an acceptance metric
is for.

**`bay-urban` measures 0.990× — shorter than the shortest legal route — and that is a
defect in the route, not a triumph over the router.** A hand-drawn GPX is not on the graph:
it cuts corners across plazas and parks along lines that are not ways, so it measures
shorter than any path a router can actually construct. Verification check 2 says the same
thing from the other direction, and has since M3: `on_network` **fails with 64 offenders**,
16–27 m off the network.

So **a detour ratio below 1.0 is a diagnostic**. It means the route is not on the network,
and two independent measurements now agree about this one. Worth stating because the
obvious misreading — "this route beat the router" — is flattering and wrong.

**A blank detour ratio is not 1.0.** `AcceptanceMetrics.detour_ratio` is `None` when nobody
measured, and the sheet prints "not measured". A 1.0 would read as "this route is already
as short as it could be", which is a claim; the truth is an absence.

**The `Scored` column is the caveat on the other two.** `fraction_lts3_plus` is a fraction
of the length that matched an OSM way, not of the route. `loop-bayarea` matched 15% of its
length, because it deliberately leaves the corridor its fixtures were frozen for — so its
0.0% is a statement about 1.2 km, not about 7.9 km. The plan sheet carries the match rate;
this table repeats it because the fraction is unreadable without it.

## The fit, and the pair count behind it

```
$ uv run longrun tune
0 preference pair(s) from a person, fewer than the 12 needed;
returning the neutral vector rather than fitting one
```

That is the current state, and `tuning.fit` is built to report it rather than to produce six
numbers anyway. Both sources §7.1 names are empty:

- **Manual GPX edits.** `pairs_from_edit` exists and `longrun repair` is already the command
  that takes an edited route. Nobody has edited one.
- **The accepted-road set from history.** `pairs_from_history` exists and `accepted_ways`
  produces the set from map-matched `osm_way_id`s (ADR 0001's retired-R4 decision). No
  history export has been ingested — M5.10's exit criterion 4 is open for the same reason.

**Why the gap is not filled with generated labels.** The scorers already read LTS. A fit
that maximised agreement with routes this project scored would recover the scorers' own
weights and report them as a runner's preference — circular, and circular in the exact
direction that would make R1 look answered. `tuning.fit` counts `synthetic` pairs separately
and never toward `MIN_PAIRS`, and `tests/eval` labels carry `by:` for the same reason
(ADR 0022).

## What the optimiser is known to do

Proven by construction, with no router and no person, in `tests/unit/test_tuning.py`:

- Given pairs generated from a known vector, it reproduces that vector's choices — on the
  training pairs and on held-out pairs it never saw.
- Given evidence with no signal, it returns neutral rather than a grid corner.
- Given twenty pairs it could win by taking a route 1.8× the direct one, **the detour
  regulariser refuses**, and a control with the penalty switched off confirms it would
  otherwise take them. That is §7.1's *"can't solve the problem by routing through every
  park"*, planted and caught.
- The full grid — 15,625 vectors, 24 pairs — runs in **0.91 s** (ADR 0020).

Recovery is asserted as behaviour, never as parameter identity. Avoiding LTS 3 and seeking
footways are the same hypothesis on evidence where every route is one or the other, so
several vectors explain the same preferences exactly; demanding the original six numbers back
is demanding the optimiser solve an unidentifiable problem. Two real bugs surfaced from
getting that wrong — a tie-break in linear space against a grid symmetric in log space, and
a tie scored as a disagreement, which made neutral score 0.0 on signal-free evidence.

## How to add evidence

```bash
# A route you edited. The edited one is the preferred one.
uv run longrun tune --edit original.gpx edited.gpx --fixtures data/

# Everything currently in tests/eval/pairs/, fitted.
uv run longrun tune
```

Twelve real pairs is the floor (`MIN_PAIRS`). Below it this document's table does not change
and the shipped model stays neutral.
