# 0035 — Tier-4 search is not built, and the reason is what the path does with what it finds

Status: **accepted**, M13, 2026-09-22.

## Context

Scope §7.10 asks for *"generic tier-4 search-and-extract with confidence ≤0.5 and a manifest
entry marked unverified"* for any jurisdiction no adapter covers. Half of that exists.
`ModelExtractor` reads a page it is **handed** and produces capped, tier-4 records; the
registry asks it for every uncovered jurisdiction; `promote.draft_adapter` turns a successful
extraction into a drafted adapter for a human to finish. What is missing is the *search*:
nothing discovers a jurisdiction's closure page, nothing fetches one, and
`ExtractionRequest` carries neither a URL nor any text.

So this is not a feature blocked on effort. It is four or five ordinary pieces of work —
discovery, a fetch through `cache.fetch` with a new tool name and args hash, HTML and PDF to
text, a field on a frozen dataclass, and cassettes — and that is exactly why it needs a
decision rather than a backlog entry.

## Decision

**Not built in M11–M13.** The blocking argument is not cost and not caution about models. It
is that until M13.5, the path a search would have fed **discarded what it found**.

`AdapterRegistry.fetch` collected features from the `AdapterResult`s in its adapter loop, and
a tier-4 extraction happens inside `_answer` with no entry in that loop — so its records went
nowhere while `JurisdictionAnswer.count` still counted them. A sheet would have said *"tier 4
answered, 3 records"* and shown no flag. Underneath that, `_extracted` passed `polygon=None`,
so had the records escaped they would have carried an empty `GeometryCollection`, made
`runs_along` return `inf`, and been dropped by `closures` as off-route. Two independent
mechanisms, one symptom, and fixing either alone would have left the symptom where it was.

M13.5 fixed both, plus a tier-4 path that spent model calls outside `MAX_ADAPTER_FETCHES`, a
`JurisdictionAnswer.confidence` that was never set, and a promotion path with no URL to
promote. **None of those had a symptom**, because `ModelExtractor` had zero instantiations and
zero tests in the whole tree. A search built before them would have been a search whose
findings vanished, whose budget was unbounded, and whose manifest entry said "unverified" only
by implication.

That is the rule this ADR records: **a source is not worth building before the path that
consumes it can be shown to carry what it produces.** The five bugs were found by writing a
test for an inert path, which is the cheapest place any of them will ever be found again.

## Consequences

**The honest-absence answer stands and is what every plan reports.** `NullExtractor` says a
model extractor exists and nothing searches for a page for it to read. Two golden routes pin
that sentence — `kc-stateline` for a park with no state qualifier, `ozarks-thin` for
`padus:OTHF` and `padus:NGO:29` — so it is a tested output rather than a placeholder.

**`ExtractedClosure.source_url` exists and is unfilled.** It is the field a promotion needs
and was the link that made `draft_adapter` unreachable from real output. Nothing produces a
value for it today, because nothing hands the extractor a page. It is declared now rather than
with the search, so the promotion path has one fewer thing to invent when somebody builds it.

**ADR 0006's redistribution rule is the constraint a search meets next.** A cassette can hold
only what may be committed. A tier-4 crawl reads whatever a county publishes, under whatever
terms that page carries, and a golden route replaying one would be redistributing it. So a
search needs a story about which pages may be recorded before it needs a crawler, and that
story does not exist.

**The extraction path is metered as if a search existed.** Tier-4 extraction now spends
`MAX_ADAPTER_FETCHES` and reports the ceiling by name, so the budget a search would need is
already in place and already tested. That is deliberate: the meter is the part that is wrong
to add afterwards.

## What would make us revisit

**A jurisdiction whose closures a plan actually needs and no adapter covers.** So far, no
route in the suite has one that matters: `ozarks-thin` is the region scope §11 predicted
would have no WZDx at all, and MoDOT's statewide feed answers for all five of its
jurisdictions at tier 1. Until a real route is hurt by the gap, the tier-4 seam is a
correctness exercise rather than a coverage one, and building a crawler for it would be
answering a question nobody has asked.

**A cheaper shape than a crawl.** Most of what a search would find is a link a region author
already knows. `RegionSpec` could carry `closure_pages: {jurisdiction_id: url}` the way it now
carries `cell_coverage` — which would give the extractor a page, exercise every path this ADR
describes, and need no discovery step, no HTML-to-text heuristics and no crawler. If tier 4
is ever wanted in earnest, that is the thing to try first, and it should be measured against
the honest-absence baseline before anything is crawled.
