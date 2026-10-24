# 0052 — A gate is judged on all its rules, an unread rule never closes it, and a park-wide rule is read where the route enters

Status: **accepted**, M18, 2026-10-23.

## Context

ADR 0046 §5 settled that an opening-hours rule is local and never converted, and left the
rule field to M18. Three things stood between that sentence and a scorer that could read a
park's hours:

1. **Opening hours were barely read.** `resupply_schedule.is_open` understood an optional
   day list without spaces and one time range. It ignored any rule it could not parse - so a
   restaurant posting `We, Th 11:00-22:00; Fr, Sa 11:00-23:00; Su 11:00-20:00`, of which only
   the Sunday rule parsed, read **closed** at Wednesday lunchtime. That broke the module's own
   first rule, "unparseable hours are unknown, never closed".
2. **A gate was one window.** `access_hours` read a feature's `start`/`end` as the hours it
   was open, one record at a time. A gate posting `06:00-12:00` and `13:00-22:00` flagged
   closed at 14:00 on its first record.
3. **A park-wide rule was read at 0 m.** A rule with no geometry was pinned to the route's
   first segment and tested at its start time, wherever the park was.

And beneath all three, one shared bug (M18.2): `closures`, `trail_status` and `access_hours`
indexed the per-point ETA list by segment number, reading every segment early.

## Decision

**1. `core/scorers/_hours.py` reads OSM `opening_hours`, and follows OSM's rule order.**
Weekday lists and ranges with or without spaces, months and dates, several time ranges,
times past midnight that spill into the next morning, `off`/`closed`/`open`/`unknown`,
comments, `24/7`, and `dawn`/`sunrise`/`sunset`/`dusk` with bracketed offsets, read
against `core/geo/solar.sun_events`. A later rule that selects the day replaces earlier
ones; a `,`-joined rule adds. `PH` and `SH` are read and never match. **One departure from
OSM:** a `;` rule with times that selects exactly the previous rule's days adds to it, so
`10:00-14:30; 17:30-20:30` is a café open for lunch and dinner, as its mapper meant and as
OSM's own validator says they meant, rather than one shut at lunch.

**2. Three answers, combined as a three-valued OR.** Open if a rule that was read opens it;
closed only if every rule was read; otherwise `None`. A rule nobody could read can no
longer be the reason a place is closed.

**3. A gate's record carries its rule in `hours`, and `start`/`end` say when the rule
holds.** The schema requires `hours` on every `access_hours` record and refuses it on any
other kind. A gate's rules are grouped by agency, name and place and judged together: of
those holding at the arrival, a dated exception beats a standing rule, and then any rule
that opens the gate opens it. No rule holding is `gate_hours_unknown`, never closed.

**4. A park-wide rule is read where the route enters the park.** `JurisdictionScan.spans`
records, per park id, where along the route its polygons are; a `{}` rule is tested at each
entry, and marked there. A park the 500 m corridor meets but the line does not enter is
entered at its nearest point.

**5. The start shift is to an opening the same local day.** A gate shut at 22:00 is bought
back by starting earlier, not nine hours later.

**6. A closure about a whole jurisdiction is a note, never a failure.** A tier 3 or 4
record with no geometry ("every trail in the county closed through the 30th") was dropped as
off-route, so the jurisdiction read as checked and the sheet said nothing. It is now a soft
route-level flag, `closure_jurisdiction_wide`. It cannot fail a route at any tier or
confidence, because check 6 reads hard flags only.

## Consequences

* Three goldens moved in M18.5, every line because a place's hours are now read:
  - `kc-stateline`: the measurements hash only. Two water points read open at the 11:00
    candidate start.
  - `phoenix-heat`: one more food waypoint, Via Della Slice Shop at Wednesday lunch.
  - `loop-bayarea`:
    - `services_with_unreadable_hours` fell from 5 to 1.
    - Four places that were counted open because they could not be read are now read, and
      are shut at 08:00.
    - One place, Joyride Pizza, went from closed to unknown, under decision 2.
* `bay-urban`, `ozarks-thin`, `boston-winter` and `synthetic-hazards` did not move.
* `resupply_schedule` and `start_time_optimizer` read hours through `_hours`, with the sun at
  the route's start; `access_hours` reads them with the sun at the gate.
* No golden carries an `access_hours` record, so decisions 3–5 moved nothing.

## What would make us revisit

A source whose rules are not expressible in this subset and matter on real routes: a park
posting `Th[4]`-style holiday exceptions that a runner would meet, or week-numbered seasons.
Before widening the grammar, measure how many records it would change.

Several NPS units on one route share `padus:NPS`, so each park's hours are tested at every
NPS entry, not just its own. Splitting spans and rules by park code would fix it. That waits
for a route where it is wrong.
