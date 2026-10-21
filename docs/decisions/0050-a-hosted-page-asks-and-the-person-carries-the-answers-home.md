# 0050 — A hosted page asks the questions, and the person carries the answers home

Status: **accepted**, 2026-10-09.

> Numbered after 0049 for the reason 0048 gives. Renumber at merge if `main` has moved.

## Context

ADR 0049 gave a person two ways to answer a preference pair: `longrun label pair` and the
Review tab, which needs the region's router, `longrun api` and the built UI running on the
machine with the ledger. Answering thirty questions means sitting at that machine with three
processes up. A page on claude.ai can be opened anywhere and keeps its own state. But it
cannot reach `~/.longrun/labels/`, and the one thing that could carry an answer from that page
to the ledger without the person is the agent, which `CLAUDE.md` and ADR 0048 keep out of the
ledger entirely.

## Decision

**A batch travels in two halves.** `longrun label batch` gathers a ledger's unanswered tasks,
in the order a session asks them (`pairs.order`, probes last), and writes:

- `BATCH.json`, the tasks themselves, into `batches/` **beside** the ledger
  (`~/.longrun/batches/`), never inside it: a batch is questions, and the ledger is answers;
- `BATCH.page.json`, what a page may hold, built from `review.task_view`: the two lines,
  where they part, their lengths, candidate ids, and the shares the page shows only once a
  question is answered. **No `drawn_by`, no stratum, no split, and no probe flag**: the
  Review tab's blindness, applied to a page that holds the whole batch at once.

**The page keeps the Review tab's rules.** An answer is a choice and a strength, "no
preference" or "can't judge"; it is final once recorded; the numbers appear after it. It
saves to the viewer's private per-person store and offers the answers as a file.

**The person carries the answers home.** `longrun label import FILE`, run by the person with
`LONGRUN_LABELER` set, adds each answered task to the ledger and records each answer through
`answers.record_pair_answer`, the function every other door ends in, with the time the person
clicked and **`via: page`**. An answer the ledger already holds is reported, never replaced
(ADR 0049: a first answer stands), so importing the same file twice records nothing new. An
answer whose candidate ids are not its task's, or from another batch, is refused.

**The agent builds the batch and the page and never touches an answer.** It may seed tasks
into a throwaway ledger and write `batches/`; it does not read the page's stored answers,
does not import, and `tools/` gains nothing.

## What this changes about where a label's evidence lives

The page holds route geometry, which ADR 0049 kept to the ledger because a task describes
ground near where somebody runs. A claude.ai page is private to its owner by default and is
not the public repository, but it is off this machine. Ends sampled from a public box (the
Marin Headlands, the Presidio) say nothing about where anyone starts; **ends drawn from a
person's own runs make sharing the page their decision**, the same decision 0048 leaves them
for promoting a route to a golden.

## Consequences

- `Via` gains `page`. Nothing reads it but people; `tune` and `publish` are unchanged.
- `label batch --limit N` takes the first N questions in session order and keeps the probes
  of exactly those, so a limited batch still checks the person against themselves.
- A batch is a snapshot. Tasks answered elsewhere after it was made are imported as "already
  in the ledger" if the answers agree and kept as the first answer if they do not.
