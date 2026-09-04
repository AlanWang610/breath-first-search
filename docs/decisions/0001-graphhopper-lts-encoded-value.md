# 0001 — LTS as a GraphHopper encoded value

Status: **in progress** (M0 spike S1). API surface verified by inspection of
`graphhopper-web-11.0.jar`; end-to-end import + query not yet run.

## Context

Scope §7.1 states, as settled fact, that an LTS 1–4 score is "computed offline per way in PostGIS
and written as an encoded value at import." This is the single highest-risk claim in the scope
(risk R1) because the rest of §7.1 — the query-time custom model whose `priority` reads LTS, and the
§7.1 parameter tuning built on it — depends on it entirely.

The widely-reported position (GraphHopper forums) is that arbitrary OSM tags cannot become encoded
values without modifying GraphHopper's source, implying a maintained fork rebased at every release.

## What was verified

Inspection of the shipped GH 11.0 jar with `javap`:

1. **`ImportRegistry` is a single-method interface:**
   `ImportUnit createImportUnit(String name)`.
2. **`GraphHopper.setImportRegistry(ImportRegistry)` is public**, alongside
   `setEncodedValuesString(String)`.
3. **`ImportUnit.create(...)` is a public static factory** taking the encoded value and tag parser
   as lambdas: `create(String, Function<PMap, EncodedValue>, BiFunction<EncodedValueLookup, PMap,
   TagParser>, String... requiredUnits)`.
4. **There is no `META-INF/services` entry for `ImportRegistry`** — so it is *not* pluggable by
   dropping a jar on the classpath. Injection must happen in code, before import.

## Decision (provisional)

**A wrapper module, not a fork.** A custom `ImportRegistry` that delegates to
`DefaultImportRegistry` for every known name and returns a custom `ImportUnit` for `"lts"`, plus a
small entry point that calls `setImportRegistry(...)` before the graph is built. This depends on
GraphHopper as a Maven artifact rather than modifying it, so upgrading is a version bump instead of
a rebase.

The LTS value itself reaches the importer as a **synthetic OSM tag** (`lts=1..4`) written into the
`.pbf` by a pyosmium rewrite, fed from the PostGIS computation. GraphHopper's tag parser reads that
tag; nothing in GraphHopper needs to know about PostGIS.

## Fallback if the wrapper proves impractical

Carry LTS in an existing parsed encoded value with ≥4 levels that pedestrian routing does not
otherwise use. GH 11 ships `track_type` (grade1–5), `smoothness`, `hike_rating`, `mtb_rating` and
`horse_rating` — all candidates. This removes the Java dependency entirely at the cost of an
obviously abusive mapping, which must then be documented loudly in the region manifest.

## Consequence that holds either way

**LTS scoring and LTS routing stay permanently separate deliverables.** `core/routing/lts.py`
computes LTS in pure Python from OSM tags and is what every scorer and the plan sheet use. The
encoded value is only ever needed for the router to *prefer* low-LTS ways. M1 needs only the former,
so no Java problem can block the core library.

## Still to verify before this is `accepted`

- Whether `GraphHopperManaged` / `GraphHopperApplication` allow the registry to be injected without
  replacing the Dropwizard entry point.
- An end-to-end import of the Bay Area extract with `lts` in `graph.encoded_values`, then a POST
  `/route` whose `custom_model.priority` reads it and measurably changes the path.
- `mvn package` time for the wrapper on this machine.

## Related finding (risk R4)

`osm_way_id` **is** a default encoded value in GH 11. Scope §6.2 defines the accepted-road set as
"map-matched OSM ways", and the concern was that GraphHopper does not expose way IDs. It does — so
map matching with `details=osm_way_id` should yield real OSM way IDs and the geometry-hashing
fallback is probably unnecessary. To be confirmed with a live `POST /match`.
