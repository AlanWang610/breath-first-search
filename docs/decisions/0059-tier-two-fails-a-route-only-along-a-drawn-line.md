# 0059 — Tier 2 fails a route only along a drawn line

Status: **accepted**, M20, 2026-10-28. Refines ADR 0013 gate 5.

## Context

ADR 0013 puts five gates between a closure and a hard flag. The fifth is "tier ≤ 2 and
confidence ≥ 0.8", and `MIN_HARD_FLAG_TIER = 2` was written so that a model reading a PDF
could never fail a route. Until M20 nothing sat at tier 2 - `state511/` was a docstring -
so the gate's tier half had only ever admitted WZDx.

M20 brings the first tier-2 sources, and they place a closure in two different ways:

- **Drawn.** An IBI Group 511 platform publishes `EncodedPolyline`: the road the event
  covers, as the publisher drew it.
- **Undrawn.** Caltrans' Lane Closure System publishes a begin point and an end point and
  nothing between them. So does an IBI event with no polyline. 36% of Caltrans' records
  have the same point at both ends.

Gate 2 asks whether a closure runs **along** the route - within 20 m and within 30° of its
bearing. A straight chord between the two ends of a curving road answers that wrongly in
both directions. A closure of Highway 1 from Bodega Bay to Jenner, drawn as a chord, crosses
the headland and meets none of the road. And a chord can lie along a frontage road it has
nothing to do with. A single point cannot run along anything, and `runs_along` already
refuses it. But a chord passes the check as easily as a road does.

## Decision

**1. A tier-2 record's confidence says whether it was drawn.**

| geometry | confidence | can clear gate 5? |
|---|---|---|
| the publisher's own line (`EncodedPolyline`, decoded) | 0.9 | yes |
| a chord between two published points, or a point | 0.75 | no |

0.75 sits under `HARD_FLAG_CONFIDENCE` (0.8). An undrawn tier-2 closure is reported, scaled
soft, and never fails a route. The rule needs no new gate: ADR 0013's fifth gate already
reads confidence, and confidence is what this record honestly has less of.

**2. A drawn record whose window over-states its schedule is held undrawn too.** An IBI
event with `Recurrence` or `RecurrenceSchedules` set carries the whole campaign as its
window, for example three weeks for a closure that holds 9 PM to 5 AM. The schedule is
prose this parser does not read. A night's closure must not fail a daytime run, so the
record is held at 0.75 until something reads the schedule.

Caltrans needs no such rule. Its "Standard" closures, 12,291 of 12,574 records, are one
record per occurrence and never longer than a day (measured 2026-10-28).

**3. Below 0.9.** A vendor's event list is a structured record but not a structured
*standard*. WZDx's 0.95 is a published spec; IBI's field list is one vendor's.

**4. What a tier-2 record's category says.** These map to the categories
`closures.blocks_pedestrians` reads:

- `IsFullClosure` or Caltrans' `Full` is `road-closure`.
- Caltrans' "Sidewalk" lane type is `sidewalk-closed`.
- Anything else is `some-lanes-closed`, or the value's own slug. A lane is not a footway.

## Consequences

* **No tier-2 record can fail a route today.** Caltrans is undrawn, and no IBI platform is
  adopted, because nobody here holds a key (`state511.AWAITING_KEY`). The rule is in place
  for the first platform that arrives.
* **Two tier-2 claimants on different ids are both asked** (ADR 0047). In San Francisco,
  511 SF Bay claims the county and Caltrans the state. Measured 2026-10-28, of 1,180
  Caltrans District 4 records live in the next seven days, 124 have a 511 SF Bay work zone
  within 300 m and overlapping in time. Of 653 SF Bay records, 53 have a Caltrans one. They
  are complements, so no `complete` flag or facet was needed to have both asked.
* **No IBI platform's payload may be committed.** None carries a grant, and no `state511_`
  family but Caltrans' is in `test_payload_licences.FAMILIES`, so their parser is tested
  against a shape fixture (ADR 0055). Caltrans is the exception, below.
* **A refused request never names its key.** `http.get_json` raises `FeedRefused(status,
  where)`, which names the host and path, in place of httpx's `HTTPStatusError`. The httpx
  message is the whole URL, so it carried 511 SF Bay's `api_key=` into coverage reasons. An
  IBI platform's refusal is said as "refused LONGRUN_..._API_KEY (HTTP 400)".

## Caltrans' conditions of use are its grant (2026-10-29)

**An exception to ADR 0038, and the user's call.** ADR 0038 commits a payload only on a
grant shipped inside it. A Caltrans Lane Closure System file carries no licence field.
What Caltrans publishes is its conditions of use, at https://dot.ca.gov/conditions-of-use,
which every CWWP2 page links as its own. Read 2026-10-29, the page is dated July 19, 2021,
and its "Ownership" section begins:

> In general, information presented on this website, unless otherwise indicated, is
> considered in the public domain. It may be distributed or copied as permitted by law.

M20 first left this sentence unread as a grant, because it is a statement about a website
and not about a payload. The user read it as a grant and decided so on 2026-10-29. This
section records that decision and how far it reaches.

* **One family, by name.** `test_payload_licences.FAMILIES` gains `state511_caltrans_`.
  - Its check reads the terms page, not the payload. The sentence is quoted verbatim there
    with its URL and read date.
  - It admits only a Lane Closure System file, and only one that indicates nothing
    otherwise: a file naming a licence, copyright, terms or rights of its own is refused,
    because the grant defers to it.
  - An IBI platform's payload is still refused under any `state511_` name.
* **Re-read live.** A `network` test fails if the sentence is reworded or withdrawn. The
  recordings are then a decision to revisit, not a fact to keep relying on.
* **Recorded.** `tests/contract/cassettes/state511_caltrans_d4.json` holds 33 District 4
  records, verbatim, chosen from the live file on 2026-10-29: two of each kind the parser
  treats differently. District 4 is also recorded into the `synthetic-hazards` cassette,
  so the route's San Francisco closures lines answer at tier 2 instead of "not in the
  cassette".
* **It does not reach further.** This is not a rule that a terms page may stand in for a
  payload's grant. A second publisher whose payload is silent needs its own decision, by a
  person, recorded the same way.

## What would make us revisit

* **A Caltrans feed that draws its closures**, such as a WZDx feed from Caltrans, or LCS
  publishing postmile-referenced geometry. Snapping the chord to the state highway network
  by route and postmile is the other way to draw one, but it is a linear-referencing system
  this project does not have.
* **A runner hitting a closure that a chord would have caught**, in an eval set. That would
  mean 0.75 is too careful for the closures that are short and straight.
* **A recurrence schedule in a parseable form.** Then rule 2 becomes a window per
  occurrence, the way Caltrans already publishes one.
* **Caltrans rewording or withdrawing the "Ownership" sentence**, or a Caltrans payload that
  names terms of its own. Either ends the exception.
