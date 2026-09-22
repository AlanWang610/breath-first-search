# Deploy

Local-first. One GraphHopper process per region plus one PostGIS instance; nothing here
assumes a hosted deployment (multi-user auth and hosted regions are explicit non-goals).

```
deploy/
  docker-compose.yml    postgis only - see below
  graphhopper/          config per region: profiles, encoded values (incl. LTS),
                        custom-model defaults, LM preparation for flexible mode;
                        run.ps1 and the LTS import module
  postgis/init/         init SQL: extensions, schema per layer, the vintage ledger,
                        and the index convention the loaders follow
```

**GraphHopper is not in the compose file.** GraphHopper 11 ships a standalone
`graphhopper-web-11.0.jar` that needs a JRE rather than a container, so it runs from
`graphhopper/run.ps1` with native access to `data/` — one process fewer to containerise,
and the LTS import module (ADR 0001) needs a JDK on the host regardless.

```powershell
docker compose -f deploy/docker-compose.yml up -d   # PostGIS on 127.0.0.1:5432
deploy/graphhopper/run.ps1                          # router on :8989
```

The init SQL runs once, against an empty data directory. Changing it and restarting does
nothing; `docker compose -f deploy/docker-compose.yml down -v` destroys the volume so it
runs again.

Region data is built by `longrun build-region` into `data/`, not baked into an image.
There are no pre-built tiles for the whole country; regions are stood up on demand.

## The one errand a build cannot run for you

Everything `build-region` needs it fetches, except **FCC mobile coverage**. No credential
is involved — the account and API token at `bdc.fcc.gov` are for the API, which a
build-time polygon layer has no use for — but `broadbandmap.fcc.gov`'s CDN answers **403
to every non-browser request**, `curl` included. So the file arrives by hand, in the same
shape the OSM extracts already do:

1. open <https://broadbandmap.fcc.gov/data-download> in a browser
2. **Mobile Broadband** → the state → Shapefile or GeoPackage (either is read)
3. save it under `data/` and name it in the region spec, keyed by state FIPS:

```yaml
cell_coverage:
  "29": ../../data/fcc_bdc/bdc_29_mobile.gpkg
# the "Data as of" date the download page prints beside the file
cell_coverage_vintage: fcc-bdc-2025-06-30
```

Until that file exists, `cell_coverage` reports `unavailable` on every plan and names this
errand as the reason. That is the correct answer and not a placeholder: *nobody has run an
errand*, *nobody wrote a loader* and *there is no signal here* are three different
statements, and a plan sheet that blurred them would be worse than one that said nothing.
The loader has existed since M13.4; the file is what is missing.

**The column names are a guess.** `national.FCC_COLUMN_ALIASES` was read off the FCC's
published field list, because nobody here has been able to download a file to check. If
yours uses different spellings the load fails immediately, by name, printing the columns it
actually found — add the real ones there. It will not quietly write a table of nulls.
