# 0004 — WBGT, by the ACSM approximation, reported as a lower bound

Status: accepted, 2026-09-10.

## Context

Scope §7.4 asks `heat_stress` for "**WBGT or UTCI** per segment from sun exposure + temp +
humidity + wind" and picks neither index. The document contains **no formula for either**,
anywhere. Two things had to be decided before the scorer could exist.

## Decision 1 — the index is WBGT

§8.3 already states the thresholds in it: soft above **26 °C**, hard above **30 °C**, with
no "(profile)" marker on either, so both are fixed floors. §6.3 names "WBGT hard threshold"
among the safety floors that cannot be lowered, and `preferences/floors.py` has carried
`WBGT_HARD_C = 30.0` since M1.3.

Choosing UTCI would orphan two numbers the scope commits to and a constant already written,
in exchange for an index whose thresholds this project has never stated. WBGT is also what
athletic governing bodies publish policy in, which is the vocabulary a runner reading a
plan sheet is most likely to already have.

## Decision 2 — the formula, and what it leaves out

**The ACSM / Australian Bureau of Meteorology approximation:**

```
WBGT = 0.567 Ta + 0.393 e + 3.94
e    = RH/100 x 6.105 exp(17.27 Ta / (237.7 + Ta))      (Magnus, hPa)
```

**It takes temperature and humidity only.** No solar term, no wind term — which is
uncomfortable, because M2 exists partly to compute exactly those two things. On an exposed
road in full sun the number it returns is a **lower bound** on what a runner meets.

The alternative is the Liljegren model, which is the reference implementation and does use
solar load and wind. It was not attempted here, and the reason is worth stating plainly:
it is an iterative radiative-balance solver, and writing one from memory without a
citation to check against would produce a number that looks authoritative and might be
wrong by degrees. A formula whose limitations are known and stated beats one whose errors
are unknown.

So `heat_stress` does two things instead of pretending:

* it reports `sunlit_fraction` beside `wbgt_c`, from `sun_exposure`'s output;
* it lowers confidence to 0.6 on any segment more than half sunlit, and says so in the
  flag detail — *"a humidity-only WBGT understates this"*.

A reader is told the figure is conservative. A reader is never handed a number that
quietly is.

## Consequences

* `heat_stress` depends on `sun_exposure` having run, which is what §7.4 already describes
  ("from sun exposure + temp + humidity + wind"). `repair._call` hands earlier results to
  any scorer that declares a `prior` parameter, and `SCORERS` is ordered accordingly.
* The scorer **under-flags** rather than over-flags in hot, sunny, dry conditions —
  Phoenix, which §11 names as test region 2 and expects to produce "WBGT hard flags in
  ordinary conditions". That region is the one that will show whether this is tolerable.
* The tier is `PHYSIOLOGICAL` (§8.4) while the threshold is a safety floor (§6.3). Not a
  contradiction, and worth writing down because it looks like one: the **tier** says what
  kind of harm it is, the **floor** says which threshold was crossed. Heat produces a hard
  flag in the physiological tier.

## What would make us revisit

Test region 2. If Phoenix in July produces WBGT figures that stay under 30 °C on exposed
arterials at midday, the approximation is failing at exactly the case the project exists
to catch, and Liljegren becomes worth implementing properly — with a reference dataset to
validate against, not from memory.

---

## Resolved 2026-09-10 — the trigger was not met, and the approximation stays

Test region 2 was asked the question this ADR set it. Open-Meteo's reanalysis for
**Phoenix, July 2026**, through `heat.wbgt_c` unchanged:

| | |
|---|---|
| midday hours sampled (11:00–16:00, whole month) | 186 |
| above the **30 °C hard floor** | **154 (83%)** |
| above the 26 °C soft floor | 181 (97%) |
| peak | **36.6 °C WBGT** — 46.3 °C at 16% RH, 24 July, 15:00 |
| coolest midday hour of the month | 24.3 °C WBGT — 32.2 °C at 11% RH |

The revisit condition was *"if Phoenix in July produces WBGT figures that stay under 30 °C
on exposed arterials at midday"*. They do not stay under it; they exceed it in five hours
out of six, by up to 6.6 °C. Scope §11's expectation of "WBGT hard flags in ordinary
conditions" is met with room to spare, and **the ACSM/BoM approximation stands**.

**What this does and does not settle.** It settles that the formula is not so conservative
as to be useless in the case the project exists to catch — which is the only claim this
ADR ever made for it. It does **not** make the number right on an exposed arterial: the
approximation still ignores solar load and wind, still returns a lower bound, and
`heat_stress` still drops to 0.6 confidence on any segment more than half sunlit and says
*"a humidity-only WBGT understates this"*. Phoenix's dry heat is in fact the friendliest
case for a humidity-only index; a humid climate at the same WBGT would be a different test.

**So Liljegren is not needed, and the reason has changed.** It was deferred because
implementing an iterative radiative-balance solver from memory would produce a number that
looked authoritative and might be wrong by degrees. It stays deferred because the
approximation demonstrably clears the bar the scope set. Should it ever be implemented, the
case to validate against is a **humid** one — Houston or Miami in August — where the
solar-and-wind terms and the humidity term pull in opposite directions and this formula's
error is not a bound in a known direction.

The measurement is kept standing as `tests/contract/test_wbgt_phoenix.py`, `network`-marked
and run on demand, rather than as a number in this file that decays the moment someone
changes `wbgt_c`.
