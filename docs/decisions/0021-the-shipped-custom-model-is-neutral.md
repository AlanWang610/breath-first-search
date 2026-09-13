# 0021 — The shipped custom model is neutral until somebody's preferences say otherwise

Status: accepted (2026-09-13)

## Context

§7.1 states that `priority` is a function of six parameters — LTS 2/3/4 multipliers, an
unpaved multiplier, a path/footway bonus and a missing-sidewalk-on-collector multiplier —
and treats their usefulness as given.

Risk R1 measured it and got a different answer. Comparing an `avoid` model
(`lts >= 3 → ×0.2`) against `neutral` on four Bay Area route pairs, the detour was **at most
0.8%**, because GraphHopper's stock `foot_priority` already keeps pedestrians off arterials.
M5.4 saw the same thing incidentally: a mild avoid model moved a detour **0 m**. M6.2
measured it a third time, now with a denominator — Ferry Building to the de Young,
`--avoid-high-stress` and neutral returning routes of **identical length**, 7694.7 m both,
against a shortest legal route of 7567.7 m.

Three measurements, no effect.

## Decision

**`NEUTRAL` — every multiplier 1.0 — is what ships.** `--avoid-high-stress` stays opt-in.
`to_custom_model(NEUTRAL)` returns `{}` rather than a model full of no-op terms, so a neutral
plan sends the router the same request it sent before any of this existed.

The plan sheet says which vector drew the route, and for a neutral one it says so in words:
*"neutral (stock foot_priority; the six scope 7.1 parameters are unfitted)"*.

## Consequences

**An unfitted parameter shipped on by default is an unmeasured assumption wearing a
measurement's clothes.** That is the whole argument. The six parameters are a reasonable
hypothesis about what makes a route pleasant on foot; three separate measurements say the
one we tried changes nothing on real Bay Area geometry; and a default that applied them
anyway would make every plan carry a claim nobody has checked.

**This is not a claim that the parameters are useless.** It is a claim that nobody knows yet,
which is a different and weaker thing. The likeliest explanation for R1's result is that the
pairs tested were ones where no alternative existed — a city grid with one bridge offers the
router no choice to express a preference over. A region with genuine parallel options, or a
runner whose edits say something the stock profile does not, could move these numbers.
`tests/eval/pairs/` is where that evidence would go.

**What would change this.** A fit over `MIN_PAIRS` or more real preference pairs that
produces a non-neutral vector with agreement meaningfully above 0.5 and a detour penalty of
zero. At that point the fitted vector becomes the default for the runner it was fitted for —
never globally, because a preference is a property of a person.

**The hard excludes are not affected and never were.** They live in the server profile
(`config-*-lts.yml`), a query model is merged with it rather than replacing it, and no vector
in the grid can reach them. A fitter that could turn off the motorway exclusion would buy
agreement on a pair by routing down a motorway.
