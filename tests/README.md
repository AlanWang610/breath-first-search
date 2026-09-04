# Tests

Scope section 11. `core/` is where the tests live; the outer layers get thin wiring tests.

| Directory | What runs there |
|---|---|
| `unit/` | Per-module tests of `core/`, pure functions with fixture geometry |
| `golden/` | Golden routes with expected measurements and flags, checked into the repo and run through the CLI with **no model in the loop**. A change in scorer output shows up here as a diff |
| `contract/` | Adapter contract tests against recorded responses in `cassettes/`. Every tier-1/2/3 adapter has one; tier-4 extraction is tested for schema and confidence bounds, not content |
| `eval/` | Small set of routes with human-judged "would you run this" labels, alongside the golden tests. Measures route quality, not correctness |

Markers: `golden`, `contract`, `network` (skipped by default).

## Test regions

Chosen for contrast on the dimensions that vary, not geography (scope 11):

1. Dense, cold, transit-rich (Boston / NYC metro) — darkness, `sun.cool`, stop density, bailouts, 1 m LiDAR
2. Hot, exposed, sprawling (Phoenix / Tucson) — near-zero canopy, long service gaps, WBGT hard flags in ordinary conditions
3. Rural, thin data (Appalachia / Ozarks) — sparse tagging, no GTFS, likely no WZDx or 511; tests whether the coverage manifest tells the truth
4. State-line-crossing (Kansas City MO/KS or Portland–Vancouver) — two DOTs, two adapter sets, one route

Plus the SF Bay Area as the development region.
