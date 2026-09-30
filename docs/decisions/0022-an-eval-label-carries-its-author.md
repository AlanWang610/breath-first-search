# 0022 — An eval label carries its author, and the suite reports how many are human

Status: accepted (2026-09-13)

**Amended 2026-09-29 (M23) — see the amendment at the foot of this file.** A human label now
has a door (ADR 0048); it carries a `labeler`, may rest on a `gap:`, and may bound more
than the first four metrics.

## Context

§11 asks for "a small eval set of routes with human-judged 'would you run this' labels
alongside the golden tests", and `tests/eval/README.md` has described it since M0 as the
suite that catches "arbitration regressions that leave every number in range but the route
unrunnable" — which is precisely the failure M5's loop made reachable.

M6 builds it, and runs straight into the thing that makes an eval set worth having: the
labels are supposed to come from a person, and no person has labelled anything. The tempting
move is to label the routes from the acceptance metrics and get on with it. That move is how
a milestone generates its own ground truth, passes against it, and reports a quality
measurement it did not make.

This project has paid for the general version of that mistake three times — M3's sabotage
that changed no output, M4's `parse_wzdx` control that agreed with its treatment, M5's
coverage test that passed against the bug it was written for. Each time the lesson was the
same: **a control that agrees with the treatment tests nothing.** A label the code wrote
about its own output is that control.

## Decision

Every `label.yaml` carries a required `by:` field with three legal values:

| `by:` | What it means | Ground truth |
|---|---|---|
| `human` | Somebody ran the route, or read the sheet and judged it | **yes** |
| `claude` | Derived from the acceptance metrics and the map, by the agent that wrote the code | no |
| `synthetic` | A route built to have a known answer | no |

`test_the_suite_reports_how_many_labels_a_person_wrote` prints the count on every run. It is
**0 of 5** today.

The same field, with the same rule, is on `tuning.PreferencePair.source`, and `tuning.fit`
never counts a `synthetic` pair toward `MIN_PAIRS`.

## Consequences

**The count is printed, not asserted.** Asserting it were above zero would fail the build for
an honest state of affairs — there is no person to label these routes today, and a red suite
that cannot be made green by writing code is a broken gate. Asserting nothing would let the
directory quietly look like ground truth. Printing puts the number in front of whoever runs
the suite, every time.

**A label must be falsifiable from the data.** A case labelled `would_run: "no"` has to
breach one of its own stated bounds, and the suite checks it. A label no number supports is
one nobody can review a month later, and one that cannot survive a scorer change — it would
be regenerated along with the thing it was meant to catch.

**Bounds are always the acceptable envelope**, never a description of what is wrong with a
route. Writing an unrunnable route's bounds the other way round — "this route has at least
15% at LTS 3" — reads naturally and makes the breach check pass without checking anything.
The first version of these labels did exactly that, and the property-1 test caught it.

**What this suite is until somebody labels a route.** A regression detector, and a good one:
sabotaging the LTS threshold to `>= 2` and rubber-stamping `--update-golden` leaves the
golden suite **green** (6 passed) and this suite **red** (`bay-urban is labelled runnable and
now breaches: fraction_lts3_plus 0.355 > 0.15`), which is the failure class it was written
for. What it is not, yet, is a measure of whether these routes are any good.

## Amendment, 2026-09-29 (M23)

The decision above is unchanged: `by:` is required and only `human` is ground truth. Four
things around it are new, because a person can now write one (ADR 0048).

**1. A human label says whose it is.** `labeler:` names the runner, and the ledger record
it was published from names them too. ADR 0021 fits a vector "for the runner it was fitted
for — never globally", and that needs the evidence to say whose it is. The same field is on
`tuning.PreferencePair`.

**2. A `no` that nothing measures is recorded with `gap:`, not refused.** Property 1 — a `no`
must breach one of its own bounds — assumes the scorers measure whatever made a runner say
no. They do not measure everything: "the path floods at high tide", "it is dull". Refusing
that label would throw away a finding about the scorers, so a `no` may instead carry
`gap: <what nothing measures>`, with or without bounds. The exception closes itself:
`test_a_gap_stays_a_gap_until_a_number_says_why` fails the moment a bound the label states
breaches, because at that point a number does say why and property 1 should be checking it.
The suite prints how many human labels rest on a gap, separately from the rest.

**3. A bound may name any of twelve metrics**, not four. To scope 7.1's three acceptance
metrics and the hard-flag count it adds scored fraction, length, climbing, unpaved share, the
water gap in minutes and in metres, the farthest point from a way out, and unlit running in
the dark — what a runner's `no` is usually about. Every one of the eight is already in every
golden's `expected.json`, which is what makes a bound on it re-checkable. The list and the
rule live in `core.labels.bounds`, shared by the suite and by the door a person writes
through, so a label accepted when it was written is refused by neither.

**4. A bound on a metric its route has no value for is refused.** It used to be skipped,
which is the silent never-ran bound `BOUND_NAMES` exists to prevent, arriving by a second
route. `loop-bayarea` had stated `detour_ratio_max: 1.15` since M6, and no golden digest has
ever carried a detour ratio for it to be checked against. The number is now in that case's
`note`, where it is a record rather than a check.
