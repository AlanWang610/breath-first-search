# 0054 — NPS parks are chosen by boundary box, and asked one at a time

Status: **accepted**, M18, 2026-10-23.

## Context

`portal.nps` claims `padus:NPS`: every National Park Service unit in the country, as one
agency. PAD-US says a route is on NPS land and not which park it is on, and the NPS API
answers per park code. The adapter therefore asked for no park at all. It got the first
fifty alerts in the country, labelled every one `padus:NPS` with no geometry, and
`trail_status` placed each on the route.

The moment a key was set, any route through any NPS unit would have flagged fifty other
parks' closures. It never showed only because no golden carries a key.

## Decision

**1. Park codes come from the NPS's own boundaries, by box.**

- **Source:** `adapters/portals/nps_units.json`, from the Land Resources Division's
  authoritative `nps_boundary` layer. It is a federal work, public domain, and was read
  2026-10-23.
- **Contents:** 429 units and 795 boxes, 30 KB, rebuilt deterministically by
  `longrun nps-units`.
- **One box per polygon part, overlaps merged.** The Appalachian Trail's single box would
  cover half a state.
- **Rounded outward,** so a box never shrinks inside its land.
- **No name matching against PAD-US's `Unit_Nm`.** That is a guess.

A box that meets the corridor where the land does not costs one park code in a request, not
a wrong flag.

**2. One request per park.** Checked live: the API reads only the first code of a list.

- `parkCode=yose,ozar` answers for Yosemite alone.
- `kica,ozar` answers for nobody.
- Both endpoints behave this way.

Each park is fetched, or replayed, and cached on its own. Pages are read in full or the
answer is a reason, never a truncated list.

**3. Boundary codes are mapped to the codes the API publishes under.** 17 of the 429
boundary codes are unknown to the API. Listing all 475 API parks and matching names
resolved them:

- 14 are published under another code (`nps_units.API_ALIASES`):
  - combined units, such as Sequoia and Kings Canyon as `seki`;
  - units published under the park that administers them, such as Fort Caroline under
    Timucuan;
  - plain differences of code.
- 3 are published nowhere, and are not asked (`UNPUBLISHED`).

**4. A corridor that meets no unit's box is a reason, not an empty answer.** Nobody was
asked, so nothing was found. With no key the answer stays the key's own reason, as before.

**5. A park's own hours become `access_hours`** (`portal.nps_hours`), cached per ISO week:

- **Which entries count:** only an `operatingHours` entry named for the park itself. A
  visitor centre's hours are not a gate's. Golden Gate posts only facilities, and so
  answers with no rule.
- **Standing hours:** one rule, with each weekday its own `;`-part.
- **Dated exceptions:** each becomes a dated rule, its local dates converted through the
  park's own zone.

## Consequences

* **Two goldens moved in M18.15.** The `padus:NPS` access_hours line in `bay-urban` and
  `ozarks-thin` now records the tier-3 step skipped for want of a key before tier 4. That is
  the same shape ADR 0047 recorded for trail status. Nothing else moved.
* **Checked live with a key against a scratch cache:**
  - The ozarks-thin corridor asked `ozar` alone: one call, its four alerts.
  - bay-urban's waterfront asked `goga` and `safr`: two calls, nine alerts.
  - Ozark and Yosemite each answered with their `All Day` park rule.
* **No NPS payload is recorded into a golden yet.** NPS data is public domain, so one could
  be. Recording it would move those lines from "key not set" to the parks' real answers,
  which is a deliberate re-record, not something to do in passing.

## What would make us revisit

- **Several NPS units on one route still share `padus:NPS`.** Each park's alerts and hours
  are therefore placed at every NPS entry (ADR 0052). Keeping the park code on each record,
  and spans per code, would fix it, once a route shows it matters.
- **The API starting to honour a list of codes.** Then the requests could collapse again.
  The one-per-park test pins today's behaviour so the change would be noticed.
