# Architecture decisions

One file per decision that was expensive to make and would otherwise be re-litigated:
`NNNN-short-title.md`, with context, the decision, and what would make us revisit it.

**What earns a file here.** A decision the scope did not make, or one it made that
implementation has since contradicted or refined. A decision the scope states clearly and
that the code simply follows does not need restating — that is transcription, and it makes
this directory look like there is work outstanding when there is not.

By that test, four of the five entries this list used to carry were transcription and have
been struck: GraphHopper over Valhalla (§4.3), no graph-workflow framework (§4.2),
elicitation in context rather than a questionnaire (§6.3), and position weight applied in
the scoring loop rather than the router (§8.2). All four are still true, still in the
scope, and still what the code does.

The fifth — SVF per corridor rather than region-wide — turned out to need a real decision
once it was measured, and is now **ADR 0005**.
