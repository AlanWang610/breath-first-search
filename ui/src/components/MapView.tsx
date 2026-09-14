/**
 * The map (scope 10.3): route, flagged segments coloured by tier, markers.
 *
 * **No basemap by default, and that is a licensing decision rather than an omission.**
 * Raster tiles come from a server with a §14 attribution obligation and usually a key, and
 * M2 took the same position for the HTML plan sheet: render fully on a blank ground and
 * treat a basemap as an enhancement. Point `VITE_BASEMAP_STYLE` at a style URL you are
 * entitled to use and it appears; without one the geometry still draws, which is what the
 * reviewer actually needs to see.
 */
import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Plan } from "../api";
import { flagsBySegment, tierColour } from "../api";

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

  useEffect(() => {
    if (!container.current || map.current) return;
    const style = import.meta.env.VITE_BASEMAP_STYLE;
    map.current = new maplibregl.Map({
      container: container.current,
      style: style ? style : BLANK_STYLE,
      center: [-122.42, 37.78],
      zoom: 12,
      attributionControl: { compact: false },
    });
    map.current.addControl(new maplibregl.NavigationControl(), "top-right");
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
    </div>
  );
}
