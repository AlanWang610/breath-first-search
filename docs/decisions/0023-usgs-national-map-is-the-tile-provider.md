# 0023 — USGS The National Map is the tile provider, and the basemap is a layer

Status: accepted (2026-09-14)

## Context

Three things in this build wanted raster tiles and none had a provider. Scope 7.9 names
`imagery_tile(lat, lon, zoom)` — "aerial tile for vision spot-check of a flagged segment;
hard cap ~10 calls per plan" — and `render(gpx, layers)`, a static map. Scope 10.3's map view
wants a basemap. `Budget.imagery_tiles_max = 10` had existed since M1, metering a cost nothing
could incur.

Twice this project declined to pick one, for the same reason. M2's HTML sheet drew its map
as inline SVG, and M7's web UI shipped with no basemap by default. Both rested on scope 14: a
tile server carries an attribution obligation, usually requires a key, and a key in a
browser bundle is a key anyone can read. "No basemap" was the right call **while every
candidate provider had those properties.**

USGS The National Map does not. Its imagery (`USGSImageryOnly`) and topographic
(`USGSTopo`) basemaps are US federal works: public domain, keyless, and credited by USGS as
"USDA, USGS The National Map". Scope 14's table already files 3DEP, NHD, PAD-US and TIGER
under "US public domain — consequence: none", and this is the same agency's product.

## Decision

**USGS The National Map is the default tile provider**, configured once in
`core/data/tiles.py` and read by every consumer:

* `LONGRUN_TILE_PROVIDER` — `usgs-imagery` (the default), `usgs-topo`, `none`, or `custom`.
  A custom provider must supply an https template *and* an attribution string; it is refused
  without one, because scope 14 makes attribution an obligation rather than a nicety.
* The web UI reads the provider from `GET /api/basemap`, not from a build-time variable, so
  there is one setting and changing it needs a restart rather than a rebuild.
* `imagery_tile` fetches through `cache.fetch`, charged to `Budget.spend_imagery_tile` inside
  the producer — ADR 0017's rule, so a replay costs nothing and an outage is never cached as
  an absence.

**The basemap is a layer under the route, never the map's style.** The map always starts
from an inline blank style and adds the provider as a raster layer beneath the segments.

## Two measurements that overrode what reading would have concluded

Taken against the live service on 2026-09-14, and pinned by unit tests and a `network` test:

* **The tile path is `{z}/{y}/{x}`**, ArcGIS's row-before-column order. San Francisco at z12
  is `1583/655`; the swapped order is a 404. Getting it backwards produces a blank map with
  no error, since a browser treats a missing tile as nothing to report.
* **Tiles stop at zoom 16**, though the service's own metadata advertises 23 levels of
  detail. z17 and above return 404 for both services. A `maxzoom` taken from the metadata
  would 404 on every request a zoomed-in map made.

The second has a sharper consequence for `imagery_tile` than for the map. A 404 means two
different things — "outside the US" and "past zoom 16" — so a request is **clamped before it
goes out**. Otherwise a spot-check asked at zoom 18 in San Francisco would cache "no imagery
here" for a place that has imagery, and that false absence would replay for good.

## Consequences

**The first version of the UI setting was wrong, and would have hidden the route.** M7's
README, and the setup steps first given for choosing a provider, pointed
`VITE_BASEMAP_STYLE` at a MapLibre *style URL*. A style URL that cannot be fetched leaves MapLibre with no style at all: `load` never fires,
so the route never draws, and a reviewer with no connection sees an empty rectangle. As a
layer, a tile that fails leaves the dark ground and the route on top of it. Nobody had set
the variable, so nothing broke when it was removed.

**The HTML sheet stays SVG.** The licence was only half of its objection; the other half is
scope 9's "no server required", and a tile fetched when the file is opened is a server. The
web UI draws a basemap; the sheet opens on a plane.

**Resolution is the real limit, and scope 12 already says so.** At zoom 16 a pixel is about
1.9 m on the ground at San Francisco's latitude. That separates a trail from a road and shows
a path through a park; it does not show a sidewalk. Scope 12: imagery "is unreliable for
fine features; used only as a capped spot-check".

**Coverage is the US only**, which is the scope's own boundary. Outside it `imagery_tile`
reports no tile at that location, and the map shows its blank ground.

**What remains blocked, and on what.** `render` moves from `decision` to `work`: the
provider exists, and compositing its tiles and the route into one image needs an image
library this build does not depend on. Scope 8.1 step 8 still does not run: it is a *vision*
spot-check, and none of the five model call sites is a vision one. Its manifest note now
names that blocker rather than the provider, so it holds whichever provider is set.

**What would change this.** A provider with better-than-16 imagery that is also public
domain and keyless would replace USGS for `imagery_tile` without touching anything but
`PROVIDERS`. A keyed street-map provider is already possible through `custom`; making one
the *default* would reopen the scope 14 question this ADR closes, and would need its own.
