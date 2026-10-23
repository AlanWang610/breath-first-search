# 0046 — A feature window is UTC, an opening-hours rule is local, and the data layer converts

Status: **accepted**, M17, 2026-09-24.

## Context

ADR 0045 found that every plan read the weather at the wrong hour, because a forecast hour
is an instant and an ETA is a wall clock, and nothing converted between them. It fixed the
forecast and the air-quality series and said nothing about the third place instants enter
`core/`: the `start` and `end` of an adapter `Feature`.

The same bug was there, in a slightly different costume. `adapters/wzdx/feed.py`'s
`parse_timestamp` read `2024-10-22T06:59:00-07:00` and `2026-09-15T12:00:00Z` correctly and
then **discarded** the offset, with a comment naming "the same simplification `forecast.py`
makes" - which ADR 0045 is the record of being wrong. The result was one list holding two
clocks: Maricopa's windows in Arizona wall time, Missouri's in UTC, both compared by
`closures`, `trail_status` and `access_hours` against a naive local ETA as though all three
were the same kind of number.

It had no symptom in any golden: `kc-stateline` and `ozarks-thin` carry real MoDOT and KDOT
payloads, and neither places a single closure within 60 m of its route. It would have had a
symptom the first time one did. A Kansas City work zone ending at 14:00Z would have been
read as ending at 14:00 CDT, five hours late, and hard-flagged a route it no longer touched.

M17 is also the milestone that begins adding adapters by the dozen (M19-M22), several of
which publish local times with no offset at all, and one kind - `access_hours` - whose
natural value is not an instant but a rule ("06:00-22:00", "dawn to dusk"). So the question
is not only where to convert, but which values are instants in the first place.

## Decision

**1. A feature's window is an instant, and it is stored as naive UTC.** `Feature.start`
and `Feature.end` say so in their field comment. `parse_timestamp` converts an offset
rather than stripping it, and a `Feature` field validator converts any aware value that an
adapter hands it - the backstop, so a new adapter that forgets cannot reintroduce two
clocks. A timestamp with no offset is taken as UTC, which is what WZDx requires publishers
to send.

**2. A publisher that sends local wall clocks declares its zone.** `adapters.base
.local_to_utc(naive, zone)` converts with the *feed's* IANA zone, per timestamp, so a
window spanning a DST change is right at both ends. Using the route's offset instead would
be wrong across that change and would make an adapter's answer depend on who asked.

**3. The conversion is in the data layer, and scorers never do the arithmetic.**
`core/data/features.py` provides `route_features(kind, jurisdictions, route, ctx)`, which
asks the registry and resolves the route's offset through the same `route_offset` the
forecast uses, returning a `RouteFeatures` whose `active_at(feature, local_eta)` converts.
All three scorers call it; none compares a window against an ETA directly. This is ADR
0045's argument carried over whole - five scorers read the forecast and three of them had
forgotten the offset - and it applies with more force here, because M18 adds a consumer
and M20-M22 add sources.

**4. `None` means the clock could not be read.** A route with no points has no offset, and
`active_at` answers `None` for any bounded window. Each scorer already has an "arrival time
unknown" branch and that is where `None` goes. A record with no window at all applies at
every instant and needs no clock.

**5. An opening-hours rule is not an instant, and is never converted.** "Gates open
06:00-22:00" means 06:00 on the local clock on every date, including the ones either side of
a DST change; converting it to UTC once would be wrong for half the year. When M18 gives
`access_hours` a rule field, the rule stays local and is evaluated against the local ETA,
while its seasonal *validity* - which is a pair of dates - uses `start`/`end` as instants.

## Consequences

* `closures.is_hard` and `access_hours.shift_to_open` take their time argument in UTC and
  say so; they stay pure functions over one frame, and the scorer converts at the call.
* No golden moved. The gates that read the window had nothing within reach to read it on.
* Tier-4 extraction (`adapters/extraction/model.py`) reads dates off a page, which are local
  wall clocks. It is not wired into any plan, and M22 converts them with the jurisdiction's
  zone when it is; until then its `_when` is the one producer still emitting local times,
  and this sentence is the marker.
* One offset per route, as ADR 0045 accepts: a route crossing a time-zone line reads every
  window on its start's clock. The largest error is an hour, at the line.

## What would make us revisit

A source whose windows are genuinely local *and* recurring - a weekly lane closure "every
Tuesday 09:00-15:00" - arriving as a closure rather than as an hours rule. That is an
instant-shaped field carrying a rule-shaped value, and the right answer is probably to give
closures the same rule field `access_hours` gets rather than to expand recurrences into
instants at parse time.
