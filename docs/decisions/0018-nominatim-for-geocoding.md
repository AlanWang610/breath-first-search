# 0018 — Nominatim for geocoding, through the cache, with a contact string the user sets

Status: accepted (2026-09-12)

## Context

Scope §8.1 step 1 turns a request into "structured constraints (start, end, date, …)", and
`cli/plan.py` has said since M3 that it "does not geocode" and that "the agent (§8.1 step
1) is where a place name becomes a coordinate". Between those two sentences is a
capability the scope assumes and never specifies: **the scope names no geocoder at all**,
gives it no licence row in §14, and sets no policy for it.

So this is a decision rather than a transcription, on the same footing as ADR 0006 (Open-
Meteo for air quality, where §7.4 named AirNow and AirNow needs a key).

The constraints that narrow it:

* §3.7 is local-first for personal data, and a route's endpoints are about as personal as
  a plan gets — a geocode of "home" is a home address.
* Every external source in this project goes through `core/data/cache.py`, and the reason
  is `LONGRUN_OFFLINE=1`: a golden route must replay rather than reach out.
* The NWS precedent is absolute and was set deliberately: a service that asks for a
  contact string gets one **the user supplies**, and this project never invents or borrows
  one.

## Decision

**Nominatim, keyless, through `cache.fetch`, gated on
`LONGRUN_NOMINATIM_USER_AGENT`.**

Three reasons it is the right default rather than merely an available one:

1. **It serves the data everything else here already runs on.** The routing graph, the
   ways layer, the LTS table and the corridor queries are all OSM. A geocoder from a
   different corpus would resolve a name to a point the router might not have a way at.
2. **No account, no key, no per-user credential** — so it needs none of the treatment
   ADR 0012 gives HPMS and M4 gives the 511 feeds.
3. **Its terms are the ones this project already complies with.** ODbL flows through, and
   the sheet already renders a share-alike trailer for OSM-derived sources.

Keyed with `STATIC_DAY`, because a place name does not change with the plan date, and
keying it by the date would re-fetch it daily for nothing.

Without a contact string, **no call is made at all**: the result is a `Geocoded` with a
reason naming the variable and the policy URL, and the budget is untouched. Nominatim's
policy caps automated use at one request a second; a plan geocodes its endpoints once and
replays them afterwards, so `MAX_LOOKUPS_PER_PLAN = 8` records a ceiling nothing
approaches rather than one anything is near.

## Consequences

**A §14 row is owed and the table has none**, so `attribution.LICENCES` gains `nominatim`
with `share_alike=True`. That is load-bearing rather than paperwork:
`tests/golden/test_golden_routes.py::test_every_recorded_source_has_a_licence` fails the
build on `LICENCE NOT RECORDED`.

**A latent bug in `forecast.py`, found by checking what a new source owes.** Its
*unanswered* sites are recorded as `source="forecast"`, and `"forecast"` is in neither
`LICENCES` nor `DERIVED_SOURCES` nor the layer map — so a plan on which every forecast
site failed would have printed `LICENCE NOT RECORDED` and failed that golden. Fixed here,
and the lesson generalised: **every source name a client can emit must resolve, including
the failure branch**, which is now asserted for both branches of the geocoder rather than
only the happy one.

**Rate limiting is not implemented and is deliberately not implemented.** One lookup per
endpoint per plan, cached, cannot approach one request a second, and a limiter that never
fires is untested code that reads as a guarantee. The day something loops over a list of
names is the day it needs one, and `MAX_LOOKUPS_PER_PLAN` is where that conversation
starts.

## What this does not decide

Whether a geocoder should be consulted at all when a region is already built. A local
resolver over the TIGER places and OSM names already in PostGIS would be strictly better
inside a built region — no network, no policy, no attribution beyond what is already
owed — and strictly useless outside one. That is a real option and it is more work than
this milestone can carry; this decision does not foreclose it.
