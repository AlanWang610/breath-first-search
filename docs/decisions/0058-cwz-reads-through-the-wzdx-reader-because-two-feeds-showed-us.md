# 0058 — CWZ reads through the WZDx reader, because two feeds showed us

Status: **accepted**, M19, 2026-10-28. Supersedes ADR 0039.

## Context

ADR 0039 declined to parse **CWZ 1.0** (Connected Work Zones). Its reason was evidence, not
effort: the one keyless CWZ feed was empty, and the three with data refused without a key.
A reader written from the specification alone would be tested against nothing. Its failure
would also be invisible: parsing to nothing reads exactly like a state with no work zones.

It named its own way back: *"A MassDOT, Colorado or Illinois key."* M19 held two of them.

## Decision

**CWZ 1.0 is read by `feed.parse_wzdx`, the reader every WZDx feed uses.** No second
parser. Both CWZ feeds with a key in hand were called on 2026-10-28:

| Feed | Bytes | Records | Parsed | Licence |
|---|---|---|---|---|
| MassDOT (`massdot__cwz`) | 3,245,157 | 1,363 | 1,363 | CC0 1.0 |
| Illinois DOT (`idot_cwz`) | 1,713,288 | 800 | 800 | CC0 1.0 |

CWZ was developed from WZDx 4.2, and its work-zone event keeps every field the reader
depends on, where it looks for them:
- `core_details` (event type, road names, description);
- `vehicle_impact`;
- `start_date` and `end_date`.

What CWZ adds is not read, and nothing in the closures path depends on it:
- `worker_presence`;
- a per-lane `status`;
- `restrictions`;
- verification flags.

The plan for this milestone named a `cwz.py:parse_cwz`. Written against these payloads, it
would have been a copy of `parse_wzdx`, so it was not written.

**What does differ is said where it shows.**

1. **The vintage names the specification.** An adapter passes `spec="cwz"` to `fetch_feed`
   and `replay_or_refuse`, and a CWZ feed reports `cwz-1.0`. Read as WZDx, it would have
   reported `wzdx-1.0`, a WZDx version from 2019.
2. **A CWZ feed that stops parsing says so as CWZ**: "the feed carried N record(s) and
   none read as CWZ". ADR 0056 made the all-records-failed case a reason. This names it.
3. **A CWZ payload is committed under its own family**, `cwz_`, with the same envelope
   check as WZDx (ADR 0038, ADR 0055).

**MassDOT's header question is settled too.** `Authorization: Bearer <key>` answered 200.
The same key as `x-api-key`, or as an `api_key` query parameter, answered 401.

## Consequences

* **MassDOT and Illinois are read.** MassDOT has been registered since M6 and was never
  checked against a payload. Illinois is adopted as `wzdx.ildot`.
* **Each backs a cassette:** both envelopes declare CC0. `cwz_massdot.json` and
  `cwz_ildot.json` replay through their adapters with no key (`test_keyed_feeds.py`).
  `test_cwz.py` holds that every recorded CWZ payload parses in full and that a CWZ
  `all-lanes-closed` reaches ADR 0013's gate 1.
* **Illinois's impact is mostly unstated.** 697 of 800 records publish
  `vehicle_impact: "unknown"`, which is Maricopa County's position: reported, and failing a
  route only if the description names a sidewalk. That is a fact about the publisher, not
  the spec. MassDOT states the impact on all but 12.
* **PurposeBuilt, the keyless CWZ feed, is still empty**: CC0, an envelope, no features. A
  `network` test re-polls it. One record in it would be a CWZ cassette nobody needs a key
  to record.
* **Colorado's CWZ feed** (`cdot_cwz`) waits in `AWAITING_KEY`, beside its WZDx feed from the
  same publisher. Adopt one of the two.

## What would make us revisit

**A CWZ record that parses wrongly rather than not at all.** The reader would take a
CWZ-only field it does not know in silence. The case that would matter is a publisher
putting the closure's real extent somewhere CWZ allows and WZDx does not, such as a lane
`status` of `closed` on every lane under `vehicle_impact: "unknown"`. If that appears, gate
1 should learn to read lanes, and it should learn from the payload that showed it.
