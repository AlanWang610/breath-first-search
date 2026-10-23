# 0047 — The tier ladder is per claimant, falls through on failure, and is paid for once

Status: **accepted**, M17, 2026-09-24.

## Context

`registry.py`'s docstring has said since M4 that the tiers are "ascending, first success
wins" — `route_forecast`'s NWS → Open-Meteo ladder applied to closures. The code did
something narrower: `_plan` found the best tier that *covered* a jurisdiction and asked only
that tier. If every adapter at it failed, the jurisdiction was reported unchecked, and a
working source one tier down was never asked. "First success wins" was really "first
claimant wins, success or not".

That was harmless with 22 tier-1 feeds and nothing below them. M19–M22 put sources at every
tier, and three things break at once:

1. **Failure did not fall through.** A tier-1 feed that is down, or keyed with no key, left
   its state unchecked while a tier-2 511 API or a tier-3 portal for the same ground sat
   unasked.
2. **One ladder per jurisdiction.** A city's ids are itself, its counties and its state.
   With one ladder over all of them, Missouri's statewide tier-1 feed would pre-empt Kansas
   City's own tier-3 street-permit portal — which describes different ground — and the
   city's source would never be asked.
3. **The fetch ceiling was paid per pass, not per question.** One registry serves every
   scorer and every rescoring pass of a plan, and the loop scores each candidate route on
   the same context. Each pass spent fresh slots on answers already in hand, so candidates
   scored later met "ceiling reached" where earlier ones met closures — and looked *better*
   in arbitration for having been checked less.

## Decision

**1. One ladder per (claimed id, facet).** For every id in `Jurisdiction.ids`, the adapters
claiming that id climb their own ladder. Arizona already needed this: `wzdx.azdot` claims
the state and `wzdx.maricopa` the county, with disjoint data sources, and both are asked
for Phoenix. An adapter may declare a `facet` to say it complements rather than substitutes
for others on the same id — a state's work zones and a city's permits — and different
facets climb separately.

**2. A failure falls through; an answer stops.** A rung whose every adapter returned a
reason hands the ladder to the next tier. An *empty* answer stops it: a feed read with
nothing in it is evidence, and the distinction between that and an unread feed is what the
whole coverage manifest rests on. An adapter may declare `complete = False` — the
Pennsylvania Turnpike claims Pennsylvania and publishes only its own road — and its answer
contributes records without ending the climb. A jurisdiction no ladder answered goes to
tier 4, which is now also true when ladders existed and all failed.

**3. A ceiling ends a ladder and never falls through.** It is a statement about this plan's
budget, not about the jurisdiction's sources, and passing it to tier 4 would spend the
thing that ran out on the least reliable source there is.

**4. A missing key is a rung nobody climbed.** The adapter is still asked, because it may
replay a recorded answer (`cache.peek`) — a cassette recorded with a key must replay in
keyless CI. With nothing recorded it returns `key_missing`, which the registry records as
`skipped`, charges nothing, and falls through.

**5. Answers are memoized per plan by what they depend on.** An adapter declares a `scope`:
`feed` (the day alone — every WZDx feed, a whole statewide file), `polygon` (the default,
because it is the safe one: a bounding-box source memoized as if it were a feed would hand
candidate B the features found for candidate A), or `jurisdictions`. Tier 4 is memoized per
(jurisdiction, kind, day), never per corridor.

**6. The ceiling is 16 live asks per kind**, replacing 24 shared. One pool let scorer order
decide which kind starved. Only live asks count: a memo hit, a missing key's peek and
`NullExtractor` cost nothing. Hitting it writes a line to `Budget.degradation`, which the
pipeline copies into the manifest.

**7. What was tried is kept whole.** `JurisdictionAnswer.attempts` records each rung as
`(tier, adapter, outcome, reason)`, and `reason` is rendered from it by one function. A
single tier of failures renders exactly as it always has — so a jurisdiction asked one
question reports that question's answer — and more than one tier names each in the order
climbed, joined by ". Then ", because a key reason already contains a semicolon. A checked
answer names its failures the long way, since there the failure is not the answer.

## Consequences

* Four golden lines moved, each to say more: SF County and SF city in `synthetic-hazards`,
  and the `padus:NPS` trail-status entry in `bay-urban` and `ozarks-thin`, now read "tier N
  <adapter>: <key reason>. Then tier 4 extraction: <reason>". Nothing else moved.
* Every WZDx adapter declares `scope = "feed"`; the three keyed adapters declare `key`, so
  a coverage report can say which adapters could actually be asked.
* `discover()` rejects a second adapter with a taken name (M17.3), because the ladder, the
  memo and the cache scope all key by name.
* A jurisdiction answered at tier 1 and failed at a peer is `checked`. Check 6 reads
  `checked`, so the peer's failure reaches the sheet only through `reason`. That is the
  same trade the registry already made for Arizona, now written down.

## What would make us revisit

A source that is complete for some kinds of closure and not others — a feed carrying full
closures but not lane closures — so that neither `complete = True` nor `False` is honest.
That is a facet question in disguise, and the likely answer is to split the adapter by
facet rather than to make completeness a function of the record.
