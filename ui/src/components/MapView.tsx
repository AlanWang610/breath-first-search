/**
 * The map (scope 10.3): route, flagged segments coloured by tier, markers.
 *
 * **The basemap is a layer, never the style** (ADR 0023). The map always starts from an
 * inline blank style, and the provider `GET /api/basemap` names is added as a raster layer
 * underneath the route. That ordering is the point. The first version took a style *URL*
 * from `VITE_BASEMAP_STYLE`, and a style URL that cannot be fetched leaves MapLibre with no
 * style at all — `load` never fires, so the route never draws either, and a reviewer with no
 * connection gets an empty rectangle. As a layer, a tile that fails to load leaves the dark
 * ground behind it and the route on top, which is the "render fully on a blank ground, treat
 * a basemap as an enhancement" rule M2 wrote for the HTML sheet.
 *
 * The provider is read from the API rather than baked into the bundle, so there is one
 * setting (`LONGRUN_TILE_PROVIDER`) for the map and for `imagery_tile`, and changing it needs
 * a restart rather than a rebuild.
 */
import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Plan } from "../api";
import { api, flagsBySegment, tierColour } from "../api";

const BLANK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#11151a" } }],
  glyphs: undefined,
};

function segmentFeatures(plan: Plan) {
  const flags = flagsBySegment(plan);
  return plan.segments.map((segment) => {
    const worst = flags.get(segment.id)?.[0];
    const points = plan.route.points.slice(segment.start_idx, segment.end_idx + 1);
    return {
      type: "Feature" as const,
      properties: {
        id: segment.id,
        colour: worst ? tierColour(worst.tier) : "#7f8c96",
        width: worst ? (worst.kind === "hard" ? 7 : 5) : 3,
        // Prose, because the hover is where somebody finds out *why* a segment is
        // coloured. A reason code alone tells a developer something and a runner nothing.
        reason: worst
          ? `${segment.id}: ${worst.scorer} ${worst.kind} (${worst.tier}) — ${
              worst.detail ?? worst.reason_code
            }`
          : `${segment.id}: nothing flagged`,
      },
      geometry: {
        type: "LineString" as const,
        coordinates: points.map((p) => [p.lon, p.lat]),
      },
    };
  });
}

export function MapView({ plan }: { plan: Plan | null }) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [basemapNote, setBasemapNote] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: BLANK_STYLE,
      center: [-122.42, 37.78],
      zoom: 12,
      attributionControl: { compact: false },
    });
    map.current = instance;
    instance.addControl(new maplibregl.NavigationControl(), "top-right");

    instance.once("load", () => {
      api
        .basemap()
        .then((basemap) => {
          if (!basemap.provider || !basemap.tiles || map.current !== instance) {
            // Said out loud rather than silently blank: a map with no ground under it
            // should explain itself, and the API sends the reason.
            if (basemap.reason) setBasemapNote(`No basemap: ${basemap.reason}`);
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
          // Underneath the route if the route is already drawn, at the bottom otherwise.
          const beneath = instance.getLayer("segments") ? "segments" : undefined;
          instance.addLayer({ id: "basemap", type: "raster", source: "basemap" }, beneath);
        })
        .catch(() => {
          // An API that cannot say which provider is no reason to lose the route.
          setBasemapNote("No basemap: the API did not answer");
        });
    });
    return () => {
      map.current?.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !plan || plan.route.points.length < 2) return;

    const draw = () => {
      const data = {
        type: "FeatureCollection" as const,
        features: segmentFeatures(plan),
      };
      const existing = instance.getSource("segments") as maplibregl.GeoJSONSource | undefined;
      if (existing) {
        existing.setData(data);
      } else {
        instance.addSource("segments", { type: "geojson", data });
        instance.addLayer({
          id: "segments",
          type: "line",
          source: "segments",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": ["get", "colour"],
            "line-width": ["get", "width"],
          },
        });
        instance.on("mousemove", "segments", (event) => {
          instance.getCanvas().style.cursor = "pointer";
          const feature = event.features?.[0];
          if (feature) {
            new maplibregl.Popup({ closeButton: false, closeOnMove: true })
              .setLngLat(event.lngLat)
              .setText(String(feature.properties?.reason ?? ""))
              .addTo(instance);
          }
        });
        instance.on("mouseleave", "segments", () => {
          instance.getCanvas().style.cursor = "";
        });
      }

      const bounds = new maplibregl.LngLatBounds();
      for (const point of plan.route.points) bounds.extend([point.lon, point.lat]);
      instance.fitBounds(bounds, { padding: 48, duration: 0 });
    };

    if (instance.isStyleLoaded()) draw();
    else instance.once("load", draw);
  }, [plan]);

  return (
    <div className="map">
      <div ref={container} className="map-canvas" />
      {!plan && <div className="map-empty">Plan a route, or pick one from the list.</div>}
      {basemapNote && <div className="map-note">{basemapNote}</div>}
    </div>
  );
}
