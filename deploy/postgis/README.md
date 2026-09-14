# PostGIS init

One schema per layer group, each with the source and vintage recorded so the plan manifest
can pin it: `osm`, `hpms`, `overture`, `padus`, `nhd`, `tiger`, `gtfs`, `user_data`.

`user_data` rather than `user`: USER is a reserved word, so a schema named `user` must be
double-quoted in every statement that touches it, and an unquoted reference fails at parse
time rather than obviously.

Vintages live in `meta.layer_vintage`, written by the loaders and read by
`LayerStore.vintage()` for the manifest (scope 6.4).

Indexes matter more than usual: every scorer starts by joining a route-corridor buffer
against a layer, and the 3-minute end-to-end budget is spent there first.
