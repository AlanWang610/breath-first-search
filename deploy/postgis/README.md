# PostGIS init

One schema per layer group, each with the source and vintage recorded so the plan manifest
can pin it: `osm`, `hpms`, `overture`, `padus`, `nhd`, `tiger`, `gtfs`, `user`.

Indexes matter more than usual: every scorer starts by joining a route-corridor buffer
against a layer, and the 3-minute end-to-end budget is spent there first.
