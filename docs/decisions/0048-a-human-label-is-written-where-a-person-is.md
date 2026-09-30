# 0048 — A human label is written where a person is, and has no MCP door

Status: **accepted**, 2026-09-29.

> Numbered 0048 rather than 0046: `main` ends at 0045, and 0046–0047 are taken on
> `m17-registry-clock-ledger`, which is open in parallel. Renumber at merge if that changes.

## Context

For eleven milestones the eval suite has printed *"eval: 0 of 5 labels written by a
person"* and `longrun tune` has returned the neutral vector with *"0 preference pair(s) from
a person"*. Both were honest. ADR 0022 made them honest on purpose: a label the code wrote
about its own output tests nothing, so `by:` is required and only `human` counts.

What 0022 did not build is a way for a person to supply one. The only doors were a
hand-edited `label.yaml` and `tune --edit ORIGINAL.gpx EDITED.gpx`. Neither is somewhere a
runner looks at a route and says what they think of it, and the first milestone to build
that door has to decide three things 0022 left open.

1. **Which door.** This project's rule is *tool and CLI before UI*: a capability lands in
   `core/` → `tools/` → `cli/` before `api/` or `ui/` gets a control. Following it for a
   label means an MCP tool that records one, and `tools/` is the agent's surface. ADR 0022
   exists to stop an agent manufacturing labels; a `record_label` tool is the most direct
   way to manufacture one there could be.
2. **Where a label lives before it is committed.** The repository is public. A verdict about
   a route somebody runs, with the plan it was written about, describes where they start from.
3. **What stops the agent that writes this code from writing labels too.**

## Decision

**A label is recorded through `core/labels/` by `cli/` and `api/` only. `tools/` has no label
tool**, and a test asserts it. This is a documented exception to the core → tools → cli
order, and the reason is the rule's own reason turned round: `tools/` is the surface for
everything the planner can do on a person's behalf, and saying what a person thinks is the
one thing it must not do on their behalf.

**The ledger is `~/.longrun/labels/`** (`LONGRUN_LABELS_DIR` overrides), beside
`~/.longrun/profile.yaml` and for the same reason: it is evidence about one runner, kept
where that runner keeps it. A verdict keeps a copy of the plan that was judged, content-
addressed and checked against the sha256 the verdict names, because a stored plan is
rewritten in place by every edit gesture and what the person saw is the fact the label is
about.

**`longrun label publish` is the only writer of labels into the tree**, and it writes only
what can be both checked and published:

- a verdict **only when it names a golden**, into `tests/eval/cases/<golden>/label.yaml`,
  where the eval suite re-checks it against that golden's `expected.json` on every run;
- a preference pair as route **summaries only** (ADR 0020's format), never geometry.

A verdict about a plan with no golden stays in the ledger. Publishing it with a frozen copy
of its numbers would raise the human count while testing nothing — numbers that can never
move can never breach — which is 0022's trap reached by a new route.

**Every record says who and how:** `by: human` (the only value the ledger's record type can
hold), `labeler` (from `LONGRUN_LABELER`, never guessed from the OS account or the git
author), `labelled_at` (passed in by the door; `core/` reads no clock), and `via: cli | ui`.

**`CLAUDE.md` carries the rule for the agent:** never record an answer, run `longrun label
verdict`/`pair` with an answer, write `by: human` into the tree, or publish.

## What this guards against, and what it does not

**It guards against an accident.** An agent asked to "fix the eval suite" or "get tune to fit"
finds no tool that records a label, no ledger in the tree it edits, and a rule telling it not
to. Each of those is a place the accident would have to go past.

**It cannot stop a determined local process.** The ledger is a directory the agent's shell
can reach, the API answers `curl`, and a YAML file can be written by anything. No mechanism
available to a single-user local install distinguishes a person's keystroke from a process
acting on their machine, and pretending otherwise would be worse than saying so.

Two mechanisms were considered and refused because they fail the person rather than the
process:

- **A TTY check on `longrun label`.** Under Git Bash's mintty, native Python sees
  `isatty() == False`, so the runner this was built for would be locked out of the CLI.
  Typer's `CliRunner` has no TTY either, so the tests would need a bypass, and the bypass is
  the agent's.
- **Refusing commits whose message carries an agent trailer.** Every commit in this
  repository carries one, including the ones a person asks for, so it would refuse the
  runner's own label commits and nothing else.

## Consequences

- A label reaching the tree is a `publish` diff a person reviews before committing, the same
  review `--update-golden` gets. `publish --dry-run` prints it first.
- A verdict on a route outside the goldens is useful the day it is written, for the ledger's
  own reports and for `tune`, and becomes a regression check only when its route is promoted
  to a golden — which is a decision about publishing its endpoints, and the runner's to make.
- The eval suite now also refuses a bound on a metric its route has no value for. That found
  `loop-bayarea`'s `detour_ratio_max: 1.15`, which had been skipped as unmeasured on every run
  since M6.

## What would make us revisit it

A second labeler with their own machine: then labels travel between installs, "who" needs
more than an environment variable, and the ledger's location stops being a guard.
