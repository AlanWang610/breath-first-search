# 0001 — LTS as a GraphHopper encoded value

Status: **accepted**. Proven end-to-end on 2026-09-04 (spike S1) against GraphHopper 11.0
and a Bay Area extract of `norcal-latest.osm.pbf`. Scope §7.1's claim survives unchanged.

## Context

Scope §7.1 states, as settled fact, that an LTS 1–4 score is "computed offline per way in PostGIS
and written as an encoded value at import." This was the single highest-risk claim in the scope
(risk R1) because the rest of §7.1 — the query-time custom model whose `priority` reads LTS, and the
§7.1 parameter tuning built on it — depends on it entirely.

The widely-reported position (GraphHopper forums) is that arbitrary OSM tags cannot become encoded
values without modifying GraphHopper's source, implying a maintained fork rebased at every release.

**That position is wrong for GraphHopper 11.** No fork is needed, and the runtime binary does not
even need to be customised.

## Decision

**A ~7 kB import wrapper, not a fork, and a stock server.**

The mechanism has two halves, and the second is the one that makes this cheap:

1. **Import** uses a custom `ImportRegistry` that delegates to `DefaultImportRegistry` for every
   name except `"lts"`, for which it returns an `ImportUnit` pairing a stock
   `IntEncodedValueImpl("lts", 3, false)` with a `TagParser` that reads the OSM tag `lts=1..4`.
   `GraphHopper.setImportRegistry(...)` is public, so this needs no source change — but it does
   need *some* Java, because the registry is only reachable in code (see "What is not possible").

2. **Serving needs nothing.** `GraphHopper.load()` never consults an `ImportRegistry`; it calls
   `EncodingManager.fromProperties(...)`, which reconstructs every encoded value from JSON stored
   in the graph's `properties` file. Because the encoded value is one of GraphHopper's own
   implementation classes rather than a subclass of our own, the **unmodified**
   `graphhopper-web-11.0.jar` deserialises and serves it.

So the custom Java is confined to a build-time step. Production runs the vendor's binary.

The LTS value itself reaches the importer as a **synthetic OSM tag** (`lts=1..4`) written into the
`.pbf` by a pyosmium rewrite, fed from the PostGIS computation. GraphHopper's tag parser reads that
tag; nothing in GraphHopper needs to know about PostGIS.

## What was proven, by running it

Every number below was measured on this machine (Windows 11, JDK 21.0.12, 64 GB RAM).

| Step | Result |
|---|---|
| Clip NorCal → Bay Area (`clip_pbf.py`, complete-ways) | 621 MB → **233 MB**, 30,152,486 nodes / 3,491,196 ways / 56,344 relations, **4 m 53 s** |
| Build the wrapper (`mvn package`, cold `~/.m2`) | **19.3 s**, jar is **7,185 bytes** |
| Write `lts=1..4` onto highway ways (`add_lts_tags.py`) | 929,417 of 3,491,196 ways tagged, **89.8 s** |
| Import **with** the wrapper | **68.2 s**, exit 0, 1,636,436 nodes / 2,174,391 edges, 361 MB on disk |
| Import the same config with the **stock** jar | fails: `java.lang.IllegalArgumentException: Unknown encoded value: lts` |
| Serve the LTS graph with the **stock** jar | loads in ~2 s, `lts` present in the loaded EncodingManager |
| `POST /route` with `details=["lts"]` | returns per-interval values 1–4 |
| `POST /route` with `custom_model.priority` on `lts` | **route changes measurably** — see below |

The graph's stored `properties` contains, verbatim:

```json
{"className":"com.graphhopper.routing.ev.IntEncodedValueImpl","name":"lts","bits":3,
 "min_storable_value":0,"max_storable_value":7,"max_value":4, ...}
```

`max_value: 4` is the load-bearing detail: GraphHopper tracks the largest value actually written,
so this is evidence the tag parser ran and stored real data, not just that the value was declared.

### The routing proof

Three requests per origin/destination pair, differing **only** in the query-time `custom_model`
(`deploy/graphhopper/scripts/verify_lts_routing.py`):

- `neutral` — no `lts` term
- `avoid` — `{"if": "lts >= 3", "multiply_by": "0.02"}`
- `seek` — the inverse, `{"if": "lts <= 2", "multiply_by": "0.02"}`

| Pair | avoid: dist / %≥LTS3 | seek: dist / %≥LTS3 | way-id overlap |
|---|---|---|---|
| SF: Ferry Building → de Young | 7,695 m / **0.0 %** | 10,863 m / **94.5 %** | 1.6 % |
| Peninsula: Stanford → Mountain View | 9,597 m / **0.1 %** | 9,571 m / **97.2 %** | 0.8 % |
| East Bay: Lake Merritt → UC Berkeley | 9,607 m / **0.0 %** | 9,869 m / **78.7 %** | 2.2 % |
| South Bay: SJSU → Santana Row | 6,688 m / **0.0 %** | 7,507 m / **93.1 %** | 1.4 % |

Two routes between the same points sharing under 3 % of their way ids, with the high-LTS share
swinging from ~0 % to ~90 %, is not a coincidence of tie-breaking. `lts` is steering the search.

Worth noting for §7.1 tuning: `avoid` and `neutral` are nearly identical on all four pairs
(detour ≤ +0.8 %). GraphHopper's stock `foot_priority` already keeps pedestrians off arterials, so
the *marginal* value of an LTS term over the default foot profile is small in dense areas. The LTS
term earns its keep on the cases the default gets wrong, not across the board — which is an argument
for fitting the §7.1 parameters against real preference pairs rather than assuming large gains.

## What is not possible (verified)

**The running server cannot be given a custom registry without replacing the Dropwizard entry
point.** `GraphHopperBundle.run()` hard-codes
`new GraphHopperManaged(configuration.getGraphHopperConfiguration())`, and `GraphHopperManaged`'s
constructor is `this.graphHopper = ...new GraphHopper(); this.graphHopper.init(configuration)` — no
setter, no factory, no HK2 binding for it. Overriding `run()` would mean copying ~80 lines of
unrelated Jersey wiring at every upgrade.

There is also **no `META-INF/services` entry for `ImportRegistry`**, so this is not pluggable by
dropping a jar on the classpath. The `setImportRegistry` call has to happen in code.

Splitting import from serving sidesteps both problems, and is why the wrapper is four classes
instead of a maintained fork of the web module.

## Build and upgrade cost

- **Build:** `mvn -f deploy/graphhopper/lts-wrapper/pom.xml package`. 19.3 s cold, ~3 s warm.
  Dependencies are `provided` and come from Maven Central (`com.graphhopper:graphhopper-web:11.0`
  is published); the runtime classpath is the wrapper jar plus the shipped fat jar.
- **Upgrade:** bump one property, `<graphhopper.version>`, drop in the new
  `graphhopper-web-<version>.jar`, rebuild the wrapper, rebuild the graph. No rebase, no conflict
  resolution. The wrapper touches seven public API symbols (listed in
  `deploy/graphhopper/lts-wrapper/README.md`); a compile break in any of them is the upgrade signal.
- **Operational constraint:** import and serve must use the **same config file**. `load()` compares
  the configured `profiles` string against the one stored in the graph and refuses a mismatch.
- **Failure mode if someone forgets:** the stock server, pointed at an `lts` config with no graph
  built yet, tries to import and dies with `Unknown encoded value: lts`. Loud, immediate, and
  unambiguous — verified, not assumed.

## Fallback status — also proven, and rejected on merit rather than on capability

The no-Java fallback carries LTS inside the stock `track_type` encoded value:
`add_lts_tags.py --also-tracktype` writes `tracktype=grade<lts>` alongside `lts=<lts>`, and
`config-bayarea-fallback.yml` lists no custom encoded value at all. This was run end-to-end, and it
works — the **stock jar imported it in 72.6 s** (no wrapper on the classpath) and served it.

It produces routes **identical to the custom encoded value**, metre for metre, on all four pairs
(7,695/10,863 · 9,597/9,571 · 9,607/9,869 · 6,688/7,507 m for avoid/seek), with the same way-id
overlaps. Confirmed with `verify_lts_routing.py --ev track_type`.

So this is a real, working escape hatch if a future GraphHopper removes `setImportRegistry` or
`ImportUnit.create`. It is **not** the chosen approach because:

- it destroys `track_type` as a data layer, which scope §7.2's `surface_profile` wants;
- `track_type` is an enum (`MISSING`, `GRADE1`–`GRADE5`), so custom models must spell out
  `track_type == GRADE3 || track_type == GRADE4` instead of `lts >= 3`. No ordinal comparison, and
  every custom model and every tuning expression in §7.1 inherits the disguise;
- it saves ~7 kB of Java and a 19-second build, which does not pay for either of the above.

One trap found while proving it, worth recording because it will bite anyone reading enum encoded
values back: **custom-model expressions spell enum values UPPERCASE (`track_type == GRADE3`) but
path details return them lowercase (`"grade3"`).** A case-sensitive reader silently reports every
segment as `MISSING` while the routing works perfectly — which looked exactly like "the fallback
does not work" until the raw response was inspected.

No foot-profile encoded value declares `track_type` as a required import unit, so overwriting it
does not corrupt pedestrian routing itself.

## Consequence that holds either way

**LTS scoring and LTS routing stay permanently separate deliverables.** `core/routing/lts.py`
computes LTS in pure Python from OSM tags and is what every scorer and the plan sheet use. The
encoded value is only ever needed for the router to *prefer* low-LTS ways. M1 needs only the former,
so no Java problem can block the core library.

## Related finding (risk R4) — retired

`osm_way_id` is a default encoded value in GH 11, and `POST /match` returns it.

A 179-point GPX track with ~5 m of synthetic GPS noise, derived from a real SF route, was posted to
`/match?profile=foot&details=osm_way_id`. It came back with **129 detail intervals covering 122
distinct OSM way ids**, 109 of which were among the 117 ways of the route the track was generated
from. Spot-checked ids resolve in the source `.pbf` to the expected features (`43100042` =
`highway=footway, footway=crossing`; `558731948` = `highway=corridor`, the Ferry Building passage).

Scope §6.2's accepted-road set can therefore be keyed on real OSM way ids. **The geometry-hashing
fallback is unnecessary and should be dropped from the plan.**

## Reproducing

```powershell
# 1. Bay Area extract (complete-ways; a naive bbox filter shreds way geometry)
uv run python deploy/graphhopper/scripts/clip_pbf.py `
    data/osm/norcal-latest.osm.pbf data/osm/bayarea.osm.pbf "--bbox=-123.2,36.9,-121.5,38.5"

# 2. synthetic LTS tag (placeholder scoring -- replace with the PostGIS lookup)
uv run python deploy/graphhopper/scripts/add_lts_tags.py `
    data/osm/bayarea.osm.pbf data/osm/bayarea-lts.osm.pbf

# 3. wrapper + import + serve
mvn -f deploy/graphhopper/lts-wrapper/pom.xml package
./deploy/graphhopper/import-lts.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
./deploy/graphhopper/run.ps1        -Config deploy/graphhopper/config-bayarea-lts.yml

# 4. the proof
uv run python deploy/graphhopper/scripts/verify_lts_routing.py

# 5. the fallback, if ever needed -- no wrapper anywhere in this path
uv run python deploy/graphhopper/scripts/add_lts_tags.py --also-tracktype `
    data/osm/bayarea.osm.pbf data/osm/bayarea-fallback.osm.pbf
java -Xmx8g -jar data/graphhopper/graphhopper-web-11.0.jar import deploy/graphhopper/config-bayarea-fallback.yml
./deploy/graphhopper/run.ps1 -Config deploy/graphhopper/config-bayarea-fallback.yml
uv run python deploy/graphhopper/scripts/verify_lts_routing.py --ev track_type
```

Note that `osmium-tool` has no usable Windows build and `winget` has no Maven package; the extract
is done with pyosmium's native `IdTracker` (installed with `uv pip install osmium`, deliberately not
added to `pyproject.toml` — it is a region-build tool, not a library dependency), and Maven 3.9.16
was unpacked from the Apache binary zip to `C:\Users\Amosq\tools\`.
