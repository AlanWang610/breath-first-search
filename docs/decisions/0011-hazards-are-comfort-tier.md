# 0011 — Hazard flags are soft, and arbitrated in the COMFORT tier

Status: accepted, 2026-09-10. Follows [ADR 0002](0002-soft-crossing-tier.md), which settled
the same question for crossings and gave the argument this one reuses.

## Context

`core/scorers/hazards.py` implements scope §7.6's last row: *"at-grade rail crossings,
tunnels, bridges with no walkway or high wind exposure, water crossings, cattle guards,
seasonal snow above an elevation threshold; NWS flood and red-flag warnings by date"*.

Nine reason codes across six sources, and **the scope gives no threshold for any of them**.
§8.3's table has hard limits for WBGT, LTS, unsignalized crossings and dry gaps; hazards
appear nowhere in it. §8.4 lists what is in the safety tier — legality, hard crossings,
LTS 4 — and hazards are not there either.

> **Erratum, 2026-09-10 (M4).** Both sentences above are wrong, and this ADR was accepted
> on them. §8.3 line 285 reads `| Legality / hazards | — | any |`, and §8.4 reads *"safety
> (legality, hard hostility, hard crossings, **hazards**, time constraints)"*. The scope
> addressed hazards in both places and I reported that it had not.
>
> **The decision below stands**, because neither of its two arguments depends on the error:
> the lexicographic one (a cattle grid in SAFETY would outrank an entire route's worth of
> dangerous heat, because arbitration never reaches the tier below) and the double-counting
> one (the disqualifying cases are already owned by `legality`, `hostility` and
> `crossings`). What changes is the reading of the scope. §8.3's row *groups* legality with
> hazards, and `legality` hard-flags the disqualifying cases in the safety tier — so the row
> is honoured by `legality`, and what `hazards` carries is the residue those three do not
> catch. That is a reconciliation rather than a contradiction, but it had to be reached by
> reading the row, not by asserting it was absent.
>
> [ADR 0013](0013-closures-may-hard-fail.md) is why this surfaced: it needed to know whether
> "no §8.3 row ⇒ no flags" was a rule of this codebase, and found that it never was.

So the tier is undecided, and a golden route is about to freeze whatever gets written. That
is the same position M1.7 was in over soft crossings, and it is why this is an ADR rather
than a default.

## Decision

**Every hazard is a soft flag in `Tier.COMFORT`.**

**The tiers are lexicographic, and that is the whole argument.** A single flag in a higher
tier beats *any* amount of evidence in a lower one, at any severity. One cattle grid in the
SAFETY tier would outrank an entire route's worth of dangerous heat, because arbitration
would never reach the physiological tier to look. ADR 0002 made this point about a
signalized crossing and it applies unchanged.

**The disqualifying cases are already owned, and promoting hazards would double-count
them.** A tunnel a runner may not enter is `foot=no`, which `legality` hard-flags in the
safety tier. A bridge on a road nobody should run on is LTS 4, which `hostility` hard-flags.
A road that cannot be crossed safely is `crossings`. What is left for `hazards` is the
residue those three do not catch — an at-grade rail crossing on a legal path, a creek with
no bridge tagged, a cattle grid — and that residue is real information without being
disqualifying.

**A hazard is a thing to know, not a thing to fail on.** Scope §6.1 calls repair mode a
diagnostic before it is an optimiser, and every hazard here is something a runner can
choose to accept. Nothing in §7.6's list makes a route unrunnable on its own.

## Consequences

* Golden expectations may pin hazard flags at COMFORT, and the tier is deliberate rather
  than inherited.
* `hazards` produces no hard flags at all, so `gpx_verify` gains no check from it. That is
  consistent: §7.9's ten checks do not mention hazards.
* An NWS alert is issued over a county or a fire-weather zone, so its flag carries
  `segment_id = "route"`. `arbitrate` gives a flag whose segment it does not recognise the
  base position weight of 1.0, which is the right answer for a condition with no position —
  §8.2's weighting exists to say that a problem at 80 km hurts more than the same problem at
  8 km, and a flood warning is at neither.

## What would make us revisit

**A hazard that is genuinely impassable rather than unpleasant.** A confirmed ford of a
river in flood is the candidate already visible: `hazards` can see the NWS flood warning and
the `ford=yes` tag on the same segment, and the conjunction is a different claim from either
alone. If that pairing is ever implemented as one finding, it belongs in the safety tier and
this ADR should be amended rather than worked around.

**A snow source worth trusting.** `SNOW_ELEVATION_M` is a blunt 2,000 m line over six
months. A route over a pass in February is a real safety matter, and the reason it is soft
here is that the evidence is weak, not that the hazard is minor. With SNODAS or a
snow-depth raster behind it the flag would be evidence about the ground rather than about
the calendar, and the tier should be re-argued then.
