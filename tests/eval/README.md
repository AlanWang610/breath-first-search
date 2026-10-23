# Route quality eval

Routes with human-judged "would you run this" labels. Separate from `golden/`, which
checks that measurements did not change: this checks whether the plan is any good.
Used to tune the ~6 custom-model priority parameters (scope 7.1) and to catch arbitration
regressions that leave every number in range but the route unrunnable.

That paragraph has been the whole of this directory since M0. M6 builds what it describes,
and the first thing to say is what is honestly here and what is not.

## A label carries its author

`label.yaml` requires a `by:` field (ADR 0022). The reason is the failure this directory
exists to prevent: a milestone that generates its own labels and then passes against them
has tested nothing. So:

| `by:` | What it means | Counts as ground truth |
|---|---|---|
| `human` | Somebody ran the route, or read the sheet and judged it | **yes** |
| `claude` | Derived from the acceptance metrics and the map, by the agent that wrote the code | no |
| `synthetic` | A route built to have a known answer | no |

`test_the_suite_reports_how_many_labels_a_person_wrote` prints the count on every run. It
was **zero** from M6 to M23. Until it is not, this suite is a regression detector and not a
measure of quality, and `docs/tuning.md` says the same thing about the fitted parameters.

**A `human` label arrives only through `longrun label`** (ADR 0048): recorded in the runner's
own ledger at `~/.longrun/labels/`, then written here by `longrun label publish`, whose diff
the runner reviews. It carries `labeler:` as well as `by:`. Nobody - and no agent - writes
`by: human` into this directory by hand.

## What a case is

```
cases/<name>/
  label.yaml     golden, would_run, reason, by, and the metric bounds that justify the label
```

A case names a route in `tests/golden/routes/` rather than carrying its own copy, and it has
to: the suite re-checks a label against that golden's `expected.json`, which moves when the
scorers do. The goldens are already the reference routes scope 7.1 asks to publish metrics
for, and duplicating their fixtures to re-measure the same numbers would buy nothing. This
README used to say a case with its own route was legal and name `unrunnable-arterial` as
one; no such case ever existed and the suite never supported one. A route of your own gets
a case by being promoted to a golden first.

A bound is `<metric>_max` or `<metric>_min` for one of the twelve metrics in
`core.labels.bounds.METRICS` - the traffic-stress numbers, hard flags, scored fraction,
detour ratio, length, climbing, unpaved share, water gaps, the farthest point from a way
out, and unlit running in the dark. A bound on a metric the route has no value for is
refused rather than skipped.

A human `no` whose reason no metric captures - "the path floods at high tide" - carries
`gap: <what nothing measures>` instead of a breaching bound. It is counted, printed
separately, and fails the suite the moment one of its stated bounds does breach, because
then a number says why and property 1 should be checking it.

## What the suite asserts, and why it is not the golden suite again

A golden pins that *numbers did not change*. Eval pins that *a plan is acceptable*. Four
properties, each one something a human judgement would rest on:

1. A route labelled `would_run: no` must fail at least one of its stated bounds, unless it
   is a `gap:` label. A label nobody can check from the data is a label that cannot survive
   a scorer change.
2. A route labelled `yes` must carry no unlisted residual hard flag. Scope 8.4 says
   residual flags are always listed; a plan worth running must not be hiding one.
3. Acceptance metrics stay inside the published bounds - so a change that doubles a
   route's LTS>=3 fraction fails here even when every scorer still agrees with its golden.
4. Every case's bounds are *falsifiable*: a bound wide enough to admit anything is
   rejected by the suite itself.

## Preference pairs

`pairs/*.yaml` holds `tuning.PreferencePair`s with their source, as route summaries - metres
per way class, never geometry (ADR 0020), which is what lets a pair about routes near
somebody's door be published from a public repository. They are what `longrun tune` reads. The same rule applies: a pair whose source is `synthetic` never
counts toward the minimum needed to fit, because it is evidence about the optimiser and
not about a runner.
