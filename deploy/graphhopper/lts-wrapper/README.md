# graphhopper-lts-wrapper

Adds a custom `lts` encoded value (IntEncodedValue, 1–4) to a GraphHopper import **without
forking GraphHopper**. Four classes, ~7 kB of jar.

Proven end-to-end against GraphHopper 11.0 on a Bay Area extract — see
`docs/decisions/0001-graphhopper-lts-encoded-value.md`.

## Why this works

GraphHopper consults an `ImportRegistry` in exactly one place: `GraphHopper.prepareImport()`,
which only runs on the **import** path. `GraphHopper.load()` instead calls
`EncodingManager.fromProperties(...)`, rebuilding every encoded value from JSON stored in the
graph's `properties` file. Two consequences:

1. **Only the import needs custom Java.** `GraphHopper.setImportRegistry(...)` is public, so
   the registry is replaceable from outside — no source change.
2. **The server binary stays stock.** As long as the encoded value is one of GraphHopper's own
   implementation classes (here `IntEncodedValueImpl`, not a subclass), the shipped
   `graphhopper-web-<version>.jar` deserialises it on load and serves it, and `lts` is usable in
   query-time custom models and in `details=lts`.

The one thing that is *not* injectable is the running server's importer: `GraphHopperBundle.run()`
hard-codes `new GraphHopperManaged(configuration.getGraphHopperConfiguration())`, and
`GraphHopperManaged`'s constructor does `new GraphHopper().init(config)` with no seam. That is why
import is a separate step rather than something the server does on first boot.

## Build

```powershell
mvn -f deploy/graphhopper/lts-wrapper/pom.xml package
```

Cold (empty `~/.m2`) that took **19 s**; warm, ~3 s. Needs JDK 21 and Maven 3.9+.
Dependencies come from Maven Central and are all `provided` — the wrapper jar contains only
its own four classes.

## Use

```powershell
# 1. build the graph WITH lts (needs this wrapper)
./deploy/graphhopper/import-lts.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml

# 2. serve it (stock jar; the wrapper is not on the classpath)
./deploy/graphhopper/run.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
```

The wrapper can also serve, if one binary is preferred:

```powershell
java -cp "deploy/graphhopper/lts-wrapper/target/graphhopper-lts-wrapper-0.1.0.jar;data/graphhopper/graphhopper-web-11.0.jar" `
     com.longrun.graphhopper.lts.LtsGraphHopperApplication server deploy/graphhopper/config-bayarea-lts.yml
```

Import and serve must be driven from the **same config file**: `GraphHopper.load()` compares the
configured `profiles` string against the one stored in the graph and refuses a mismatch.

## Where the number comes from

`OSMLtsParser` reads a synthetic OSM tag `lts=1..4` off each way. That tag is written into the
`.pbf` before import by `deploy/graphhopper/scripts/add_lts_tags.py`. Scope §7.1 computes the
real value in PostGIS (Furth: speed × lanes × separation, AADT as modifier); the script currently
holds a crude tag-only placeholder, and swapping it for a PostGIS lookup keyed on OSM way id
changes nothing in this module.

## Upgrading GraphHopper

1. Bump `<graphhopper.version>` in `pom.xml` — it is the only version reference.
2. Drop the new `graphhopper-web-<version>.jar` into `data/graphhopper/` and update the
   `-Jar` defaults in `run.ps1` / `import-lts.ps1`.
3. `mvn -f deploy/graphhopper/lts-wrapper/pom.xml package`.
4. Rebuild the graph (the stored encoding version is checked on load and will refuse an old graph).

The API surface this depends on is small and all public:

| Symbol | Used for |
|---|---|
| `GraphHopper.setImportRegistry(ImportRegistry)` | injecting the registry |
| `ImportRegistry` (single method `createImportUnit(String)`) | the registry itself |
| `DefaultImportRegistry` | delegation for every non-`lts` name |
| `ImportUnit.create(name, evFactory, parserFactory, required…)` | describing `lts` |
| `IntEncodedValueImpl(String, int bits, boolean)` | the stored value |
| `TagParser.handleWayTags(int, EdgeIntAccess, ReaderWay, IntsRef)` | reading the tag |
| `GraphHopperServerConfiguration`, `GraphHopperBundle`, Dropwizard `ConfiguredCommand` | reusing GraphHopper's own YAML parsing |

A compile break in any of these is the upgrade signal. There is no `META-INF/services` entry
for `ImportRegistry`, so this cannot be done by dropping a jar on the classpath — the call to
`setImportRegistry` has to happen in code, which is what `LtsImportCommand` exists for.
