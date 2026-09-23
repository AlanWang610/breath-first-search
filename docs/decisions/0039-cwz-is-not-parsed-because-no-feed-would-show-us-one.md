# 0039 — CWZ 1.0 is not parsed, because no reachable feed would show us one

Status: **accepted**, M14, 2026-09-23.

## Context

Four feeds in the USDOT registry publish **CWZ 1.0** — Connected Work Zones, developed from
WZDx but a different specification — and `adapters/wzdx/feed.py` has never parsed one.
`massdot.py` has carried the warning since M6:

> It may well read; nobody with a key has yet seen whether it does, and a feed that answers
> and parses to nothing would look exactly like a state with no work zones.

M14's job was to widen adapter coverage, and Massachusetts, Colorado and Illinois are three
states sitting behind that one unknown. So the question was put properly: write the second
reader, or decide not to and say why.

## Decision

**CWZ 1.0 is not parsed.** Not because it looks hard, and not on a judgement about effort.
Because **this project cannot obtain a single CWZ payload containing a single feature**, so
any reader written for it would be written against the specification and tested against
nothing — which is the thing `azdot.py:11` exists to forbid: *"Reading the documentation is
not the same as calling the endpoint, and only the second is evidence."*

All four were called on 2026-09-23:

* **`pbs`, PurposeBuilt Systems** — the only keyless one. **HTTP 200, 599 bytes.** A
  well-formed envelope declaring `version: "1.0"`, a CC0 dedication, a data source named
  "Digital Traffic Control Diary" — and `"features": []`. It has no jurisdiction either:
  the registry's state for it is `n/a`.
* **`cdot_cwz`, Colorado DOT** — **HTTP 403**, `Not Authorized`.
* **`idot_cwz`, Illinois DOT** — **HTTP 422**, `{"loc": ["query", "api_key"], "msg": "Field
  required"}`.
* **`massdot__cwz`, MassDOT** — **HTTP 401**, `{"message": "No API Key provided."}`.

So the one feed that will talk to us is empty, and the three that have data will not. A
reader built on this evidence would be a guess at a schema, shipped into the path that
decides whether a route is closed, and it would fail in the one way the coverage manifest
cannot see: parsing to nothing reads identically to a state with no work zones.

## Consequences

**Massachusetts stays exactly where M6 left it.** `wzdx.massdot` remains registered, tier 1,
reporting `LONGRUN_MASSDOT_API_KEY is not set` by name. Nothing about it changes and nothing
about it is now known that was not known before — which is itself the finding, because the
obvious reading of "M14 widened adapter coverage" is that Boston got better and it did not.

**`feed.parse_wzdx` would silently return `[]` for a CWZ payload, and the caller would
half-notice.** `client.fetch_feed` calls a feed unavailable only when it has *neither*
features *nor* an envelope *nor* a publisher. The PurposeBuilt payload has an envelope and a
publisher, so if a CWZ feed were ever wired up as it stands, the result would be
`AdapterResult(features=[], reason=None)` — the shape that means "read, and there was
nothing here". Written down because it is the failure mode, and because the guard that
looked like it would catch it does not.

**The registry's CWZ count moved and is worth restating.** M14's plan recorded four CWZ
feeds: Colorado, MassDOT, PurposeBuilt and Illinois IDOT. The registry read on 2026-09-23
agrees exactly — four rows at `CWZ 1.0`, one keyless. This is the rare case where a stale
snapshot was still right.

**This is ADR 0035's rule applied to an input rather than to an output.** That ADR declined
tier-4 search on the grounds that *"a source is not worth building before the path that
consumes it can be shown to carry what it produces"*. The symmetry: a reader is not worth
building before there is a payload that can be shown to exercise it. Both refusals are
about evidence, and both are cheap to reverse the moment the evidence arrives.

## What would make us revisit

**A CWZ payload with features in it, obtained lawfully.** Any of three routes:

1. **A MassDOT, Colorado or Illinois key.** All three are free registrations. The moment one
   is in a `.env`, that feed's real payload can be read, and the reader can be written
   against it. `massdot.py` already records that its own header-vs-query-parameter
   authentication is unverified, so the first person with a key has two questions to answer,
   not one.
2. **PurposeBuilt publishing anything.** It is keyless and CC0, so a single work zone in
   that feed would be both a schema to read and — under ADR 0038 — a payload that could be
   committed as a cassette. Worth re-calling occasionally; it costs one HTTP request.
3. **The spec's own examples, if they carry a licence.** The CWZ specification publishes
   sample documents. If those are licensed for reuse they would serve to write the reader,
   though not to prove any publisher matches them — which is a weaker claim than a cassette
   and should be labelled as one.

**What would not be enough:** the specification alone. That is what M14 refused, and the
reason is one line long in `azdot.py`.
