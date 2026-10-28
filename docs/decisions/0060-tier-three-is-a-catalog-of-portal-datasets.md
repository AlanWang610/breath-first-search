# 0060 — Tier 3 is a catalog of portal datasets, mapped in five kinds, with every large place decided

Status: **accepted**, M21, 2026-10-28.

## Context

Scope 7.10 puts open-data portals at tier 3 and names Socrata, ArcGIS and CKAN. The
nationwide-adapter scope asked for the 50 largest metros to be covered at tier 3, with
each metro either adopted or refused with a reason.

A city's closures on a portal are not a new kind of source each time. They are two
platforms, asked the same way, with different column names. Written as Python, that would
be fifty modules differing only in strings, and every bug fixed fifty times.

## Decision

**1. A dataset is a row in `adapters/portals/catalog.yaml`.**

- `catalog.py` turns each adopted row into a tier-3 adapter.
- One entry point, `portal-catalog`, registers all of them through `registry.expand`
  (M17.5), so a bad row is one named `LoadFailure`, never a catalog that vanishes.
- A row records:
  - where the dataset is;
  - how its columns map onto a `Feature`;
  - the licence its own metadata declared, verbatim;
  - what the adopting survey measured;
  - the freshness past which an answer is refused.

**2. The mapping language has five kinds, and that cap is the design.**

- The kinds are `field`, `const`, `map`, `template` and `point`.
- Each slot accepts only the kinds that make sense for it.
- Units (`mph`/`kph`) and timestamp formats (`date`, `local`, `utc`, `epoch_ms`,
  `epoch_ms_date`) are attributes of `field`: they say what a column holds, and compute
  nothing new.
- A row needing a sixth kind is a validation error. Lookups, arithmetic and regular
  expressions mean the dataset's meaning is not in its columns, and its home is a Python
  adapter a reviewer can read.

**3. One client, two dialects.**

`client.fetch_portal` keeps the rules `wzdx/client.py` set:

- **The cache holds the platform's rows, not features.** A corrected mapping must not need
  re-recording.
- **The cache key is `{adapter, snapped box, day}`.** The box is snapped outward to 0.01°,
  so nearby routes share one recorded answer. No credential is in the key.
- **Refused, never truncated.** A query needing more than `MAX_PAGES` pages is a reason.
- **Stale is a reason.** The dataset's own metadata is read once per ISO week. A dataset
  not updated within its row's `max_staleness_days`, or one that cannot say when it was
  updated, answers nothing. A table nobody maintains still returns rows, and "checked,
  nothing here" from it is the false negative `massdot.py` names.
- **A map service with no `editingInfo` may name a `freshness_field`.** That column's
  newest value then dates the table (Phoenix).

Each platform asks in its own dialect:

- **Socrata** asks for the corridor with `intersects`, `within_box` or `between`, by how
  the row maps geometry. It asks for the window in SoQL. A Socrata app token is sent when
  set; it is never an `adapter.key`, because a declared key whose value is missing skips
  the rung, which would skip every Socrata dataset in CI.
- **ArcGIS** asks with an envelope, `f=geojson`, and pages while the server says
  `exceededTransferLimit`.

**4. Confidence by kind.**

- **A closure is 0.7,** the NPS adapter's figure, below `closures.HARD_FLAG_CONFIDENCE`.
  `MIN_HARD_FLAG_TIER` forbids tier 3 from failing a route anyway.
- **A speed survey is 0.85.** Its number is a measurement the agency took, and ADR 0053
  merges nothing under 0.8. At 0.7, a survey could never reach the graph it was adopted
  for.

**5. "Metros" are the 50 most populous incorporated places.**

- They are `regions.coverage.largest()` over the committed national table, as M17.6
  chose. There is no CBSA source.
- Each is decided for closures and for speed surveys: an adopted row claims it, or a
  `refused:` row does.
- A refusal gives a reason from a closed set, `catalog.RefusalReason`:
  - `no_dataset`
  - `no_portal`
  - `no_geometry`
  - `no_window`
  - `stale`
  - `points_only`
  - `not_a_survey`
  - `platform_unbuilt`
  - `not_public`
  - `covered`
- A refusal also lists what was checked.
- A test enforces all of this. A `covered` refusal must name a tier-1 or tier-2 adapter
  the registry really has.

**6. `longrun survey-portal` reads and never writes.**

- It prints a dataset's metadata, its licence verbatim, its columns, and a stub row.
- It can search a Socrata domain or ArcGIS Online inside a box.
- The decision and the jurisdiction are a person's.

**7. The README's ground rule is restated.**

- **Before:** "The only state-specific code lives in `adapters/jurisdictions/`". No such
  directory ever existed.
- **Now:** "Jurisdiction-specific code and data live only under `adapters/`". That is
  where the WZDx modules, the NPS units and this catalog all already are.

## Consequences

* **12 closure rows adopted for 11 of the 50 places:**

  | Place | Platform | Rows |
  |---|---|---|
  | New York | Socrata | 2: blocks and intersections, on separate facets |
  | Chicago | Socrata | 1 |
  | Dallas | Socrata | 1 |
  | Seattle | Socrata | 1 |
  | San Francisco | Socrata | 1 |
  | Phoenix | ArcGIS | 1 |
  | Nashville | ArcGIS | 1 |
  | Raleigh | ArcGIS | 1 |
  | Virginia Beach | ArcGIS | 1 |
  | Atlanta | ArcGIS | 1 |
  | Louisville | ArcGIS | 1 |

  The other 39 are refused. Each was answered live on 2026-10-28.
* **One speed survey adopted: Oakland's 85th-percentile tube counts.** It is the first
  claimant M18's `speed_surveys` build step has had. 49 places are refused.
* **One golden moved, `synthetic-hazards`:** its San Francisco line now names the tier-3
  rung, unrecorded on purpose. `kc-stateline` did not move, because Kansas City, Missouri
  publishes no closure dataset.
* **Not built: CKAN, USFS and RIDB.**
  - No CKAN portal among the fifty answered an automated search with JSON, and none of
    their datasets were verified adoptable.
  - The USFS Enterprise Data Warehouse host did not resolve from the surveying machine.
  - RIDB needs a key nobody holds.
  - All three wait for a survey that can reach them.

## What would make us revisit

- **A dataset that needs a sixth mapping kind and is worth having.** The answer is a
  Python adapter beside the catalog, not a sixth kind.
- **Portals publishing WZDx directly.** Austin already does. Each such city moves to
  tier 1, and its catalog row becomes `covered`.
