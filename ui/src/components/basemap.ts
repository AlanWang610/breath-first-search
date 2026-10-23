/**
 * The basemap, as a layer under whatever a map draws (ADR 0023).
 *
 * Moved out of `MapView` in M23, when `PairMap` became a second map that needs the same
 * ground and none of `MapView`'s camera or gesture handling. The rule it carries is the one
 * `MapView`'s docstring argues for: the provider is added as a raster *layer* on a blank
 * style, so a tile that fails leaves the dark ground and the lines on top of it, and a map
 * with no ground under it says why.
 */
import type maplibregl from "maplibre-gl";
import { api } from "../api";

export function attachBasemap(
  instance: maplibregl.Map,
  options: {
    /** The layer to insert beneath, if it exists yet; the bottom otherwise. */
    beneath: () => string | undefined;
    /** Where to say that there is no basemap, and why. */
    note: (text: string) => void;
    /** False once the map this was called for has been replaced or removed. */
    stillCurrent: () => boolean;
  },
): void {
  api
    .basemap()
    .then((basemap) => {
      if (!basemap.provider || !basemap.tiles || !options.stillCurrent()) {
        // Said out loud rather than silently blank: the API sends the reason.
        if (basemap.reason) options.note(`No basemap: ${basemap.reason}`);
        return;
      }
      instance.addSource("basemap", {
        type: "raster",
        tiles: basemap.tiles,
        tileSize: basemap.tile_size ?? 256,
        // MapLibre stops requesting past this and scales the last real tile up. USGS
        // measured at 16: past it every request is a 404.
        maxzoom: basemap.maxzoom ?? 16,
        attribution: basemap.attribution,
      });
      instance.addLayer({ id: "basemap", type: "raster", source: "basemap" }, options.beneath());
    })
    .catch(() => {
      // An API that cannot say which provider is no reason to lose the lines.
      options.note("No basemap: the API did not answer");
    });
}
