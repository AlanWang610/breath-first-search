# 0061 — A portal payload is committed only beside the metadata that dedicated it to the public domain

Status: **accepted**, M21, 2026-10-28. Extends ADR 0038 by its own revisit condition.

## Context

ADR 0038 commits a payload only when the publisher ships a CC0 grant inside it, in a
WZDx feed's `feed_info.license`. It acts on nothing else.

A portal row has no envelope. A Socrata dataset's grant is in its own metadata:
`licenseId` in `/api/views/<id>.json`. An ArcGIS layer has no licence id at all. Its
item's `licenseInfo` is free HTML. ADR 0038's revisit section anticipated exactly this
case: a publisher stating its grant somewhere other than the payload.

## Decision

**1. A portal payload is committed beside the grant.** It goes in
`tests/contract/cassettes/portal_<name>.json`, beside `portal_<name>.meta.json`. That file
holds the dataset's metadata **verbatim, key by key**, so a reviewer reads the publisher's
words and not this project's summary of them.

- **What is kept:** the licence-bearing keys - `id`, `name`, `attribution`, `licenseId`,
  `license`, `rowsUpdatedAt`.
- **What is left out:** the contact fields. They name individual city staff, and a
  repository is the wrong place to republish anyone's email address.

**2. Only a public-domain dedication qualifies.** `catalog.COMMITTABLE_LICENCES` lists
Socrata's `CC0_10`, `PDDL` and `PUBLIC_DOMAIN`, and nothing else qualifies:

- **CC-BY:** an attribution licence is a permission with an obligation that a committed
  fixture would carry forever.
- **"See Terms of Use":** a permission to read, not to republish.
- **ArcGIS:** never qualifies, because its licence is HTML that no test can read as a
  grant.

CKAN's `cc-zero`, `odc-pddl` and `other-pd` are the same grants. They join the set when a
CKAN client exists.

**3. The rule reaches the goldens.** A golden's `cache.sqlite` is a committed payload too.
An `adapter.portal.*` row may be recorded into one only for a dataset whose catalog row
declares a committable licence. A test reads every golden cassette to check this.

**4. A committable licence is necessary, not sufficient.** The committed rows are chosen,
not dumped. San Francisco's special-traffic permits are named for their applicants, often
a person, so the committed San Francisco rows are special events. A synthetic golden is
not recorded against real data at all. `synthetic-hazards` crosses San Francisco, whose
closures are public domain, and its line says "not in the cassette": real closures on a
hand-written route would add flags nobody wrote.

## Consequences

* **Two portal payloads are committed, four rows each:**
  - Seattle's street closures, `PUBLIC_DOMAIN`;
  - San Francisco's temporary street closures, `PDDL`.

  A contract test maps each through its catalog row, with nothing dropped. So the parser
  meets each publisher's real rows in CI.
* **No ArcGIS payload is committed.** Those parsers are tested on hand-built rows in
  `test_portal_client.py`, which is ADR 0055's answer for a family that cannot be
  recorded.
* **`test_payload_licences.py` checks a file by path, not by payload.** A portal family's
  grant lives beside the file, not in it.

## What would make us revisit

- **A portal publishing a machine-readable licence for ArcGIS layers.** A layer field, or
  a Hub item licence id, could join `COMMITTABLE_LICENCES` the day one exists.
- **A dataset whose metadata licence and portal-wide terms disagree.** For example, a
  dataset marked public domain on a portal whose terms of use forbid bulk republication.
  The dataset's own declaration is what this decision acts on, as ADR 0038 does. A
  conflict found in the wild would need counsel, not a test.
