# kc-stateline — two DOTs, two adapter sets, one route

Scope §11's test region 4: *"One state-line-crossing route (Kansas City MO/KS or
Portland–Vancouver OR/WA): two DOTs, two 511 systems, two adapter sets on one route; catches
jurisdiction-discovery bugs."*

3,195 m west to east along the 47th Street grid at latitude 39.0430, from **Johnson County,
Kansas** across State Line Road (−94.6083) into **Jackson County, Missouri** — about 1.4 km
in Kansas and 1.8 km in Missouri. Everything else about the route is ordinary on purpose.
Its whole job is to be crossed by a state line.

## Why Kansas City and not Portland–Vancouver

§11 offers both. Kansas City wins on one fact checked before the region was built: **both
DOTs publish keyless WZDx feeds under CC0.** Oregon's needs an API key, and ADR 0006
established that a source whose data cannot be redistributed cannot be committed as a
cassette — which would have left the golden unable to replay the half of the route that
matters.

It also wins on a hazard worth having in a test: **there is a Kansas City in each state, and
they share a border.** `JurisdictionAnswer.label()` renders a name *and* an id because of
exactly this — neither alone identifies one here.

## What it demonstrates

Nine jurisdictions resolve from the frozen `boundaries` and `parks` layers. Five are census
boundaries and all five answer at **tier 1**:

| jurisdiction | answered | by |
|---|---|---|
| Johnson County (`tiger:county:20091`) | tier 1 | `wzdx.kdot` |
| Jackson County (`tiger:county:29095`) | tier 1 | `wzdx.modot` |
| Roeland Park (`tiger:place:2060825`) | tier 1 | Kansas, `covered_by` |
| Westwood (`tiger:place:2077500`) | tier 1 | Kansas, `covered_by` |
| Kansas City (`tiger:place:2938000`) | tier 1 | Missouri, `covered_by` |

Two DOT feeds, **two HTTP requests, 1,190 work zones**, and every place inside each state
answered by its state's single fetch. That is `registry._plan`'s whole reason for existing,
and a route crossing a state line is the only shape that shows it.

The four PAD-US agencies — `padus:CITY`, `padus:NGO`, `padus:OTHS`, `padus:PVT` — have no
*closure* adapter and fall to the tier-4 seam, which reports honestly that no model is wired
up. Note that `padus:CITY` carries **no state qualifier** here: with Kansas and Missouri
both in play, `jurisdictions_from_frames` refuses to guess which one a city park belongs to.
That is the degradation path `test_a_park_on_a_two_state_route_stays_unqualified_rather_than_picking_one`
describes, happening for real.

**Check 6 skips**, because four of nine jurisdictions were not checked. Under ADR 0013 that
is the correct answer: fail beats skip beats pass, and a route where a third of the
jurisdictions went unasked has not been cleared.

## Zero closures is the true answer, and here is the evidence

`closures` finds nothing on this route despite fetching 1,190 features. That is not a
filtering bug and it was checked rather than assumed — the nearest work zone of any kind is
**1.83 km** from the route, and the nearest `all-lanes-closed` is **3.75 km** away on US 69,
a highway no pedestrian is on. `CLOSURE_BUFFER_M` is 20 m and nothing is considered past
60 m.

The route was **not** moved to intersect a closure. Fitting a route to whatever roadworks
existed on the recording day would make the golden a test of that week's traffic rather than
of jurisdiction discovery, and the five gates between a work zone and a hard flag are
already exercised against real recorded payloads in `tests/contract/test_wzdx.py`.

## Pins

`2026-09-15`, a Tuesday, 08:00 local, UTC−5 (Kansas City observes daylight saving; late
September is CDT). Five days after recording rather than ten, because **Open-Meteo's air
quality endpoint forecasts about seven days where its weather endpoint reaches sixteen** — a
date inside the weather window but outside the air-quality one records a cassette that
half-scores, which the first attempt at this golden did.

The route is hand-drawn along a real street grid rather than router-drawn, as `bay-urban`
is: no GraphHopper graph has been built for this region. `check_2 on_network` reports what
that costs.

The cassette is permanent. A forecast for a past date cannot be re-fetched, and neither can
a work-zone feed — a WZDx endpoint serves current conditions and has no archive.
