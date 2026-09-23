# 0043 — GTFS-RT departure boards are not built, and `transit_at` stays honestly absent

Status: **accepted**, M16, 2026-09-23.

## Context

Scope §7.7 lists `transit_at(point, time)` — *"departures at start/end/bailout points"* —
sourced from *"GTFS for every agency intersecting the region; **GTFS-RT where published**"*.
M16 was the milestone to do it in: it is the time-dependent milestone, it touched
`core/data/gtfs.py` anyway (M16.4), and the tool is registered and reporting itself absent.

`core/data/gtfs.py` already argued for the shape the project has instead — a per-stop
*service summary* rather than a timetable — and said where the missing piece belongs:

> What this gives up is `transit_at`'s exact departure board (§7.7). That is a tool rather
> than a scorer, it needs GTFS-RT to be worth much, and it should be added as its own path
> when something needs it — **not smuggled in as a table nothing can read**.

The temptation is to read "its own path" as "small". It is not, and the reasons are
structural rather than budgetary.

## Decision

**Not built in M16.** Four independent obstacles, each of which would have to be answered.

**There is no seam a per-trip lookup can arrive through.** Every method on the `LayerStore`
protocol is spatial — `ways_in_corridor`, `points_in_corridor`, `polygons_intersecting`,
`lines_crossing`, plus `has_layer` and `vintage`. A departure board is a lookup by
`(stop_id, time)`, which is not one of them, and that is exactly why the static `stop_times`
table was rejected in the first place: a table no method can query is a table nothing can
read. Adding a board means adding a fifth kind of method to the one protocol both stores and
every scorer are written against, or bypassing the store entirely — and bypassing it is how
a layer stops appearing in the coverage manifest.

**RT is delta-encoded against a static feed, so it is two data paths, not one.** A
`TripUpdate` carries a `trip_id` and a `stop_time_update` with a delay or a replacement time;
without the static `stop_times` it is amending a timetable nobody has. So a departure board
needs *both* — the static table this project deliberately does not load, and a live protobuf
feed — and the second is useless without the first. "Add GTFS-RT" is therefore two features
whose cheaper half was already decided against on its own merits.

**The cache is keyed `(tool, args_hash, day)`, and a departure board is not valid for a
day.** `core/data/cache.py` chose that key because *"almost everything cached here is a
forecast or a closure list, which are only meaningful for the day they describe"*. An RT feed
is meaningful for about thirty seconds. Under the existing key, the second call of a plan
would serve the first call's board; under `LONGRUN_OFFLINE=1` a golden route would replay a
board recorded on a frozen past date and present a departure that left months ago as the next
one. Neither failure is loud. A new cache tier with a TTL is a change to the one door every
external call goes through, and that door is what makes the goldens hermetic.

**A recorded RT cassette is a redistribution question ADR 0006 has already answered for
closures.** A cassette is a committed copy of a publisher's feed, and most agency RT
endpoints carry terms of use rather than a grant. So the parse path could not be covered by a
golden even once it existed, which is the position `wzdx.azdot` is in.

**`tests/unit/test_tools.py` is the tripwire and it stays armed.** `ABSENT = {"transit_at",
"place_notes", "render"}` asserts that the tool is registered, reachable, and says why it
cannot answer — M10 removed `cue_sheet` from that set by building it and M11 removed
`pin_waypoint`. Leaving `transit_at` in it is the mechanism this repository uses for "the
scope names it and this build cannot do it", and it is a better answer than a board built on
four unanswered questions.

## Consequences

* **`transit_at` keeps its current reply**, which names the reason and the blocker:
  GTFS is loaded as a per-stop service summary rather than as `stop_times`.
* **Nothing in the plan pipeline is waiting on this.** `transit` and `bailouts` ask "is this
  stop useful at this time", which the summary answers; no scorer asks for a next departure.
  M16.4 made that path *more* correct rather than less, by reading `calendar_dates.txt`.
* **`gtfs-realtime-bindings` stays out of the dependency set**, and with it protobuf. That is
  not the reason for this decision, but it is a real consequence: the hermetic suite installs
  no protobuf runtime today.
* **The four obstacles are ordered.** The cache TTL is the one to solve first, because it is
  the only one that would affect every other live source the project ever adds; the store
  seam is second, because it decides whether a board can be reported in coverage at all.

## What would make us revisit

**A scorer that needs a departure, not a service span.** `bailouts` is the candidate: "can I
get out at 32 km" is currently answered with "a stop here is running at this hour", and
"the next bus is in 47 minutes" is a materially better answer. If that scorer is ever written
to want it, the board acquires a consumer and the store seam has to be faced anyway.

**A cache tier with a TTL.** If any other live source arrives — a live closure feed, a
real-time AQI reading — the `(tool, args, day)` key has to gain a shorter-lived sibling, and
at that point three of the four obstacles above are down to two.

**A publisher whose RT feed carries a redistribution grant.** One CC0 agency feed would make
a cassette possible and a golden route testable, which would turn this from a path that
cannot be covered into one that can.
