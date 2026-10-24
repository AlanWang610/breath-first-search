# 0055 — A shape fixture is written from documentation, and a payload is committed only under a family that grants it

Status: **accepted**, M18, 2026-10-23. Extends ADR 0038.

## Context

ADR 0038 allows a payload to be committed only when the publisher ships a CC0 grant inside
it, and left a gap open in its revisit section: the feeds it refuses are parsed by code that
nothing hermetic tests. M19–M21 widen that gap by the dozen:

- 511 platforms, many of whose terms forbid republishing;
- city portals whose licences are free-text HTML;
- keyed feeds whose envelopes say nothing.

Separately, the test that enforced ADR 0038 globbed `wzdx_*.json`. A 511 platform's payload
committed as `state511_ga.json` would have passed every test there was.

## Decision

**1. Every committed payload belongs to a family that states its grant.**
`tests/contract/test_payload_licences.py` reads every file in `cassettes/`:

- Each file's name must start with a prefix in `FAMILIES`.
- Each family names the check its payload must pass. WZDx and CWZ require CC0 in the
  envelope (ADR 0038).
- A family with no committable grant is simply not listed, so its payload cannot be
  committed under any name.

**2. Otherwise, a shape fixture.** `tests/contract/shapes/<family>_<name>.json` is a payload
written by hand from the vendor's published documentation, with invented values, under a
`_provenance` block:

- `family`;
- `written_from`, the documentation's https URL;
- `read_on`;
- `"values": "invented"`.

It carries the field names, nesting and documented value formats a parser depends on, and
none of the publisher's data. Each one is checked twice:

- **Hermetic:** a test parses it.
- **Live:** a `network` test asserts the live payload's keys still include the fixture's, so
  a fixture that drifts from reality fails somewhere.

**3. A shape fixture is never derived from a recorded payload.** That would be a cassette
with its licence filed off. A test refuses a name that is both.

## Consequences

* The CC0 scan moved out of `test_wzdx_breadth.py`.
* `shapes/` starts empty, with a README. The first fixtures arrive with M19's CWZ and OHGO
  readers and M20's 511 platforms.
* ADR 0038's own caveat stands. A shape catches a renamed or moved key and not a value the
  documentation got wrong, which is why every documented format (epoch units, offsets,
  local versus UTC) is written into the fixture, not left to a default.

## What would make us revisit

A publisher, or counsel, saying a hand-written shape of their payload is itself a derivative
work. Nothing here assumes it is not. Every value is invented, and the structure is what
their public documentation already publishes.
