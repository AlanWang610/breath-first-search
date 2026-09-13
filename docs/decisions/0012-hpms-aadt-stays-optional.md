# 0012 — HPMS AADT stays optional, and spike S2's measurement could not be made

Status: accepted, 2026-09-10. Closes risk R2 by its own pre-authorised fallback rather than
by the measurement the build plan asked for.

## Context

The build plan's risk R2 reads:

> **HPMS AADT ↔ OSM conflation.** The literature is blunt: no general method, error-prone,
> hard to verify. But §12 already concedes AADT covers arterials and collectors only, and
> tag-only LTS is a published, defensible approach (Wasserman et al., TRR 2019). **Decide
> now:** AADT is a confidence-raiser, never a requirement — `lts_from_tags(tags, *,
> aadt=None)`. Spike before M3: match HPMS CA against OSM `primary|secondary|trunk` for one
> county and report match rate. If it is under 60% in the Bay Area, cut the feature and say
> so in §12.

The spike was attempted. **It could not be run, because the data is not publicly
reachable.** Checked on 2026-09-10:

| endpoint | answer |
|---|---|
| `fhwa.dot.gov/policyinformation/hpms/shapefiles/ca2022.zip` (also 2021, 2020) | `404` |
| `geo.dot.gov/.../services/HPMS_Public_Release` | `{"code": 499, "message": "Token Required"}` |
| `geo.dot.gov/.../services/ARNOLD_Inventory_HPMS` | `Token Required` |
| `geo.dot.gov/.../services/HPMS_Measure` | `Token Required` |
| the Caltrans open ArcGIS organisation (167 services) | no AADT or traffic-volume service |

This is not a claim that HPMS is unobtainable — a person with an account, or willing to
work a portal by hand, can very likely get it. It is a claim that **no unauthenticated
endpoint serves it**, which is what a region build would need.

## Decision

**AADT stays exactly what R2 pre-authorised: an optional confidence-raiser, absent by
default.** `lts_from_tags(tags, aadt=None)` already has that shape and has since M1.5, so
this ADR changes no code. What it changes is the status of the question: it is settled by
the fallback, not left open pending a spike that cannot be run.

**Scope §12 already says the right thing** — AADT covers arterials and collectors only —
and this adds the sharper version: *no AADT is loaded for any region, and none is planned
until a source is reachable without credentials.* `hostility` reports `aadt: null` on every
segment of every route today, which is the honest form of that.

**The measurement is not abandoned, it is blocked on a key.** Should HPMS become reachable,
the spike is unchanged and its threshold stands: match HPMS against OSM
`primary|secondary|trunk` for one Bay Area county, and if the rate is under 60%, cut the
feature outright rather than shipping a conflation nobody can verify.

## Consequences

* `lts_from_tags`' `aadt` parameter, `_common.AADT_COLUMNS` and `hostility`'s `aadt` value
  stay. They cost nothing, they are tested, and they are the seam a future loader plugs
  into. Removing them would have to be undone to add the loader back.
* `regions/build.py` records `hpms (no loader written)` among the sources step 2 did not
  load, and step 5's coverage report says so per region. A reader of a Phoenix or Bay Area
  build sees the absence rather than inferring it.
* **The LTS level a plan reports is tag-only, everywhere, today.** Wasserman et al. is the
  defence for that being a reasonable thing to publish, and it is what `hostility`'s
  reasons list already shows: `highway:primary; no_sidewalk; lanes_4` and no volume term.

## What would make us revisit

A reachable volume source of any provenance — HPMS through an account, a state DOT's own
traffic census, or OSM's own sparse `aadt`/`traffic_signals:volume` tagging. The first two
would need the spike run before anything is wired; the third needs no conflation at all,
because it is already on the way, and would be the cheapest thing to try first.
