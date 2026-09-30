# 0049 — A preference pair is two lines the router drew under competing vectors

Status: **accepted**, 2026-09-29.

> Numbered after 0048 for the reason 0048 gives. Renumber at merge if `main` has moved.

## Context

`tuning.fit` needs `MIN_PAIRS` (12) or more pairs from a person before it returns anything
but the neutral vector (ADR 0021). Scope §7.1 names two sources, a runner's own GPX edits
and the roads their history says they run, and both are empty on this install: nobody has
edited a generated route, and no Strava export has been ingested. ADR 0048 gave a person a
door to label through. This decides what a pair *is* when it arrives through that door,
because every choice below is a way the answer could stop being the person's.

## Decision

**Both lines are the router's.** A request - two ends somebody might run between - is drawn
under six vectors (`seeding.SEED_VECTORS`: neutral, the documented `--avoid-high-stress`,
and a seek and a shun for paths and for unpaved surface), plus the router's own
alternatives to the neutral line. A pair is only evidence about the six parameters if its
two lines differ in what those parameters can see (ADR 0020), and lines the router drew
under different vectors are exactly that. A line a person drew by hand is `tune --edit`'s
business, not this.

**A question has to be worth asking.** Two lines are kept only if they are different routes -
at most 85% of either runs along the other - *and* different mixtures of way classes, by at
least 150 m; two lines through different streets of one class are the same pair to a fitter
that sees nothing but metres per class. Of the pairs that remain, those the six-parameter
grid is most split on (`split_fraction` over 3^6 vectors) are kept, **one per stratum and two
per request**: two questions about the same ends are correlated evidence, not twice the
evidence. A session asks round-robin across strata, so one that stops early has asked about
more than one parameter. This is ordering, not active learning; for the few dozen pairs one
person will answer, it is enough.

**Blind.** Which vector drew which line is kept in the ledger and never shown. A and B are
fixed when the task is made, by the candidates' ids, which are random - never by the vector,
or "neutral" would always be A.

**Numbers after.** A task carries each line's shares of LTS 3+, unpaved and path, and a door
shows them only once the person has answered. Shown first, the choice would follow this
project's own LTS reading back into the fit, which is the circularity `docs/tuning.md`
refuses to generate labels over.

**No preference is not a pair.** "No preference" and "can't judge" are kept and counted -
the no-preference rate on pairs that are distinct by construction is itself evidence about
whether the parameters matter where somebody runs (R1) - but `PreferencePair` has no way to
say "neither", and inventing one would put an answer in the fit nobody gave. A choice carries
a strength: `clear` is weight 1.0, `slight` is 0.5.

**A first answer stands, and a second look is a probe.** An answer is never overwritten; a
later mood does not quietly replace a judgement. Instead `--repeat` re-asks a few tasks with
A and B swapped, **after every other task**, and `label status` reports how many agree. A fit
resting on answers that disagree with themselves is resting on noise, and the number that
says so is printed where the person looks.

**Recorded as `source: judged`, with the labeler.** It counts toward `MIN_PAIRS` - it is a
person's - and it carries whose it is, because ADR 0021 fits a vector for one runner.

## Consequences

- A pair on ground where every street is calm is a pair about surface and paths. `docs/tuning.md`
  measured the LTS terms moving a city route by under 1%; the Marin Headlands lines M23 drew
  moved path share from 28% (neutral) to 92% (seeking paths). Which strata a person's ground
  can ask about is a property of the ground, and `label seed` prints it.
- Sampled ends land in water and on open hillside. Both lines start where the router snapped,
  so a snap does not make a question wrong, and a task records the snapped ends; only a snap
  over 500 m - a point in the bay that snapped to the other shore - is refused.
- **The graph has no elevation**, so two lines may differ in climbing that neither the fit nor
  the question controls. A person will see it on the ground and weigh it; the fit will
  attribute that weight to whatever class the climbing ran on. Known, and not fixed here.
- Tasks hold geometry near where somebody runs, so they live in the ledger (ADR 0048). What
  reaches the tree is `publish`'s: summaries named by candidate id, never a coordinate.
