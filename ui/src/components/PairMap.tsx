/**
 * Two lines on one map, for choosing between (ADR 0049).
 *
 * A separate component from `MapView`, which carries M12.4's camera rules and every gesture
 * handler; none of that belongs here, and a shared component would make each change to one
 * page a risk to the other. What is shared is the ground: the blank style and the basemap as
 * a layer (`basemap.ts`), for ADR 0023's reasons.
 *
 * **The lines are offset, not stacked.** Where they run together, one drawn on top of the
 * other would hide it, and the stretch both routes share would look like one route's alone.
 * Offset a few pixels each way, a shared stretch reads as two lines side by side, and the
 * stretches where they part are drawn heavier so the question — this way or that way — is
 * the thing the eye lands on.
 *
 * Fitted once per task, like `MapView` fits once per plan: a person who zooms in to look at
 * one junction has not asked for the view to jump back.
 */
import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { ReviewTask } from "../api";
import { LINE_COLOURS, boundsOf, lineFeatures } from "../lib/review";
import { attachBasemap } from "./basemap";
import { BLANK_STYLE } from "./MapView";

const EMPTY = { type: "FeatureCollection" as const, features: [] };

export function PairMap({ task }: { task: ReviewTask | null }) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const fittedFor = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [basemapNote, setBasemapNote] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: BLANK_STYLE,
      center: [-122.42, 37.78],
      zoom: 11,
      attributionControl: { compact: false },
    });
    map.current = instance;
    instance.addControl(new maplibregl.NavigationControl(), "top-right");
    instance.once("load", () => {
      for (const which of ["a", "b"] as const) {
        const offset = which === "a" ? -3 : 3;
        instance.addSource(`line-${which}`, { type: "geojson", data: EMPTY });
        instance.addSource(`diverges-${which}`, { type: "geojson", data: EMPTY });
        instance.addLayer({
          id: `line-${which}`,
          type: "line",
          source: `line-${which}`,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": LINE_COLOURS[which], "line-width": 3, "line-offset": offset },
        });
        instance.addLayer({
          id: `diverges-${which}`,
          type: "line",
          source: `diverges-${which}`,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": LINE_COLOURS[which], "line-width": 7, "line-offset": offset },
        });
      }
      attachBasemap(instance, {
        beneath: () => (instance.getLayer("line-a") ? "line-a" : undefined),
        note: setBasemapNote,
        stillCurrent: () => map.current === instance,
      });
      setReady(true);
    });
    return () => {
      instance.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!ready || !instance) return;
    for (const which of ["a", "b"] as const) {
      const features = task ? lineFeatures(task[which], which) : { line: EMPTY, diverges: EMPTY };
      (instance.getSource(`line-${which}`) as maplibregl.GeoJSONSource).setData(features.line);
      (instance.getSource(`diverges-${which}`) as maplibregl.GeoJSONSource).setData(
        features.diverges,
      );
    }
    if (task && fittedFor.current !== task.id) {
      const bounds = boundsOf(task);
      if (bounds) instance.fitBounds(bounds, { padding: 56, duration: 0 });
      fittedFor.current = task.id;
    }
  }, [ready, task]);

  return (
    <div className="map pair-map">
      <div ref={container} className="map-canvas" data-testid="pair-map" />
      {!task && <div className="map-empty">No question on the map.</div>}
      {basemapNote && <div className="map-note">{basemapNote}</div>}
    </div>
  );
}
