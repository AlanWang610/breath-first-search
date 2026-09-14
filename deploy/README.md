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
