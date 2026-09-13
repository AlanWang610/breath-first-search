# Adapter contract tests

Every adapter satisfies the same contract: `fetch(polygon, date)` returns Features with
geometry, start/end, category, confidence, and source URL, or reports unavailability
rather than raising. Recorded responses live in `cassettes/`, one per adapter and case,
including the failure cases (feed down, schema drift, empty result) — those are the ones
that decide whether the coverage manifest tells the truth.
