# 0002 — Soft crossings are arbitrated in the COMFORT tier

Status: accepted, 2026-09-09.

## Context

Scope 8.4 arbitrates lexicographically over three tiers — safety, physiological, comfort —
and lists what belongs in the first two. Crossings appear in the scope under 7.3 as a
measurement, but 8.4 never says which tier a crossing flag lands in. `crossings.py`
already splits them:

* a **hard** crossing — an unsignalized crossing of a road at or above the speed floor —
  is `Tier.SAFETY`, which 8.4 does cover;
* a **soft** crossing — signalized, or below the speed floor — was assigned
  `Tier.COMFORT` when the scorer was written, with no decision behind it.

M1.7 forces the question. A golden `expected.json` pins arbitration output, so leaving the
tier unsettled means the first golden route records an accident, and any later change to
the tier rewrites expectations for reasons unrelated to the change under review.

## Decision

**Soft crossings stay in `Tier.COMFORT`.**

The tiers are lexicographic, not weighted: a single flag in a higher tier beats *any*
amount of evidence in a lower one, regardless of severity. Promoting soft crossings to
SAFETY would therefore mean a route with one signalized crossing loses to a route with
dangerous heat exposure — the arbitration would never reach the physiological tier to
notice. That is not a defensible reading of scope 8.4, whose whole point is that the tiers
encode kinds of harm that are not commensurable.

The honest argument on the other side is that crossings are where pedestrians are actually
struck. But that argument is about the *density* of crossings along a route, not about any
one of them, and density is already what `stop_density.py` measures. A route where soft
crossings accumulate to something dangerous is a route the density scorer flags.

## Consequences

* `crossings.py` is unchanged; the tier it already assigns is now deliberate.
* Golden expectations may pin soft-crossing flags at COMFORT.
* Hard crossings keep their SAFETY tier, and the speed floor
  (`CROSSING_HARD_SPEED_KPH`) is the only lever that moves a crossing between the two.

## What would make us revisit

A density-triggered promotion — soft crossings above some crossings-per-km threshold
becoming a SAFETY flag in aggregate — is the one extension that would not break the
lexicographic argument, because it would flag the accumulation rather than the individual
crossing. If the eval set in M6 shows runners rejecting routes that this arbitration
accepts, and the rejections cluster on crossing-dense routes, build that. Do not simply
move the tier.
