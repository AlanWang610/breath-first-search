# Deploy

Local-first. One GraphHopper container per region plus one PostGIS instance; nothing here
assumes a hosted deployment (multi-user auth and hosted regions are explicit non-goals).

```
deploy/
  docker-compose.yml    graphhopper + postgis, to be written
  graphhopper/          config per region: profiles, encoded values (incl. LTS),
                        custom-model defaults, LM preparation for flexible mode
  postgis/              init SQL: extensions, schema per layer, indexes on the
                        route-buffer joins that every scorer runs
```

Region data is built by `longrun build-region` into `data/`, not baked into an image.
There are no pre-built tiles for the whole country; regions are stood up on demand.
