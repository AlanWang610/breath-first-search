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

## Amendment, 2026-09-30 (M23)

The decision stands: NEUTRAL ships. Three things about what would change it were wrong, and
the first two would have made the change impossible or misleading.

**1. "Agreement meaningfully above 0.5" is the wrong bar.** NEUTRAL costs a route by its
length, so it is not a coin flip - it is "prefer the shorter line", and it agrees with every
runner who chose the shorter line. A fitted vector earns its place by beating *that*, on pairs
it was not fitted to. `tuning.holdout_agreement` measures it: K-fold, folds fixed by the
pairs' own names, pooled, with a 95% Wilson interval, and NEUTRAL and AVOID_HIGH_STRESS
scored on the same folds. The bar is now: **the interval's floor above NEUTRAL's held-out
agreement**, with at least `MIN_PAIRS` real pairs and a detour penalty of zero.

**2. A fitted vector that sought anything could not have shipped.** GraphHopper refuses a
priority above 1 and refuses the whole request, so every seek - half the grid, and any
profile saying `surface: dirt` - came back "no route". It was never noticed because nothing
consumed a fitted vector. Seeks are now written as avoids on the complement, which keeps
every ratio the fit reasons about (A CORRECTION, M23).

**3. The fit ignored a stated surface.** `_surface_terms` multiplies the fitted `unpaved`
and `path_bonus` by the profile's stated surface at query time; the fit costed pairs without
it, and so would have learned a stated preference a second time. Both now read
`custom_model.effective_params`.

The evidence the ADR was waiting on has a source now: a person's own judgements, through
`longrun label` (ADR 0048, 0049), recorded with whose they are so a vector can be fitted for
one runner and never for everybody.

## Amendment, 2026-10-23 (M24)

"Becomes the default for the runner it was fitted for" now has a mechanism: **ADR 0051**. The
vector lives in the runner's profile (`routing`), they save it with `longrun tune --save`,
and only after six gates pass, the last of which re-draws every pair with the router. NEUTRAL
still ships, and is still what anyone without a saved fit is drawn with. The sentence this ADR
promised on the sheet is now written, for neutral and fitted vectors alike.
