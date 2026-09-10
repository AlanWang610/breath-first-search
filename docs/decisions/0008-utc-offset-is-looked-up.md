# 0008 — The UTC offset is looked up, not derived from longitude

Status: accepted, 2026-09-10. Supersedes the interim rule in `core/geo/solar.py`.

## Context

A plan carries a **naive local** start time. `FrozenClock` takes one, `request.yaml` pins
one, and `PlanRequest.start_time` is a bare `time`. Solar position depends on the actual
UTC instant, so something has to supply the offset.

M2 shipped an interim answer: take it from `--utc-offset` if given, otherwise derive it
from longitude, and report which. That was enough to land `sun_exposure`, and it was never
good enough to keep. Longitude gives mean solar time, so it is wrong by a full hour
wherever summer time is in force — which for a US route is most of the running season:

| | true offset, 12 Sep | longitude says |
|---|---|---|
| San Francisco | −7 | −8 |
| Boston | −4 | −5 |
| Phoenix | −7 | −7 |

**An hour is more than fifteen degrees of solar azimuth** — measured at 25° near noon —
which moves a building's shadow from one side of a street to the other. That is the
difference between a segment reported as shaded and the same segment reported as exposed,
which is the measurement M2 exists to make.

Phoenix is the instructive row. Arizona does not observe daylight saving, so the rule of
thumb is accidentally correct there — and Phoenix is scope §11's hot test region, the one
most likely to be used for checking heat behaviour. "It looked right when I tried it" is
not evidence about a timezone rule.

## Decision

**Resolve the zone from the coordinate and ask it for the offset on the plan's own date.**

`tzfpy` maps a coordinate to an IANA zone name; `zoneinfo` — standard library, system
tzdata — answers what that zone's offset was on that date, so summer time is handled
rather than averaged over. `utc_offset_for(lat, lon, when, stated)` returns
`(hours, how)`, and `how` is one of three:

* `stated` — the caller passed `--utc-offset`. Never second-guessed: a user has told us
  something the database cannot know.
* `zone America/Los_Angeles` — looked up.
* `derived from longitude` — the fallback, kept only because a lookup can fail on a
  coordinate no zone polygon covers, and a wrong hour reported as a guess still beats a
  crash.

Which of the three happened reaches the coverage manifest on every plan and the summary
measurement of both `sun_exposure` and `lighting`. Scope §3.6 applied to the clock.

`tzfpy` was chosen over `timezonefinder` on packaging: abi3 wheels for cp310+ across
fourteen platforms, against three.

## Consequences

* A new runtime dependency, and a small one. It is a base dependency rather than an extra
  because `sun_exposure` and `lighting` are base scorers.
* Ambiguous instants inside a DST transition resolve to standard time, which is
  `zoneinfo`'s default. Wrong by an hour for at most one hour a year, and only for a run
  starting inside that hour.
* **`bay-urban` deliberately does not pin an offset**, so the lookup is exercised end to
  end on a real coordinate and its expectation records `zone America/Los_Angeles`. If a
  tzdata release ever changes a US offset, that golden is where it becomes visible.
  `synthetic-hazards` keeps its pin, so one route stays reproducible against a frozen
  number and the other tracks reality.

## What would make us revisit

A region build (M3) knows its own polygon, so the offset becomes a property of the region
rather than something resolved per run — at which point this becomes a build-time lookup
and the per-run path is a fallback for ad-hoc routes. The three-state reporting should
survive that move unchanged.
