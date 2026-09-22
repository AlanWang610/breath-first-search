/**
 * The map (scope 10.3): route, flagged segments coloured by tier, markers, and from M12 the
 * gestures that change a plan.
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
 *
 * **Two lifecycle bugs had to go before any gesture could work**, and they are M12.4.
 *
 * *Handlers closed over the first plan they saw.* They were registered inside the draw
 * effect's `else` branch — the one that runs only when the `segments` source does not exist
 * yet — so they were created once, against the first plan rendered, and never replaced.
 * There was no `instance.off()` anywhere in the file. A read-only map got away with it
 * because the only thing a handler did was show a popup whose text came from the feature.
 * The moment a handler has to *send* a range, it has to send the current plan's.
 *
 * The fix is a single registration at map construction, reading through `latest`, which an
 * effect with no dependency list refreshes after every render. Registering per plan and
 * unregistering in a cleanup would work too and is worse: it churns listeners on every
 * poll tick, and a missed `off()` is then a silent duplicate rather than a loud absence.
 *
 * *`fitBounds` refit on every plan change.* Which is correct when somebody opens a
 * different plan and destroys their work when the same plan comes back from a write — an
 * edit round-trips through a job and a poll, so the first lock a user set would have
 * thrown away the pan they set it from. Fitted once per plan **id** now, with a button for
 * when they want it back. A camera the user moved is state, and a re-render is not a
 * reason to discard state.
 */
import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Plan } from "../api";
import { api } from "../api";
import {
  elevationRuns,
  flagsBySegment,
  pointAtDistance,
  segmentRange,
  tierColour,
  type Vertex,
} from "../lib/plan";

const BLANK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#11151a" } }],
  glyphs: undefined,
};

/** What a click on the map means right now. `null` is "select a segment". */
export type DrawMode = "via" | "avoid" | "alternative" | null;

export interface Selection {
  startM: number;
  endM: number;
}

function segmentFeatures(plan: Plan) {
  const flags = flagsBySegment(plan);
  return plan.segments.map((segment) => {
    const worst = flags.get(segment.id)?.[0];
    const { startM, endM } = segmentRange(segment);
    const points = plan.route.points.slice(segment.start_idx, segment.end_idx + 1);
    return {
      type: "Feature" as const,
      properties: {
        id: segment.id,
        // Carried on the feature so the click handler needs no lookup — and metres rather
        // than the id, because `segment_id` is positional and an id read before an edit
        // names different ground after one (M11.3).
        start_m: startM,
        end_m: endM,
        flagged: worst ? 1 : 0,
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

function emptyCollection() {
  return { type: "FeatureCollection" as const, features: [] };
}

function selectionFeature(plan: Plan, selection: Selection | null) {
  if (!selection) return emptyCollection();
  const points = plan.route.points.filter(
    (point) => point.cum_dist_m >= selection.startM && point.cum_dist_m <= selection.endM,
  );
  if (points.length < 2) return emptyCollection();
  return {
    type: "FeatureCollection" as const,
    features: [
      {
        type: "Feature" as const,
        properties: {},
        geometry: {
          type: "LineString" as const,
          coordinates: points.map((p) => [p.lon, p.lat]),
        },
      },
    ],
  };
}

function draftFeature(mode: DrawMode, vertices: Vertex[]) {
  if (vertices.length < 2) return emptyCollection();
  const ring = vertices.map((v) => [v.lng, v.lat]);
  return {
    type: "FeatureCollection" as const,
    features: [
      {
        type: "Feature" as const,
        properties: {},
        geometry:
          mode === "avoid" && vertices.length > 2
            ? { type: "Polygon" as const, coordinates: [[...ring, ring[0]]] }
            : { type: "LineString" as const, coordinates: ring },
      },
    ],
  };
}

export function MapView({
  plan,
  drawMode,
  vertices,
  selection,
  cursorM,
  onPickSegment,
  onVertex,
}: {
  plan: Plan | null;
  drawMode: DrawMode;
  vertices: Vertex[];
  selection: Selection | null;
  cursorM: number | null;
  onPickSegment: (segment: { id: string; startM: number; endM: number }) => void;
  onVertex: (vertex: Vertex) => void;
}) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const popup = useRef<maplibregl.Popup | null>(null);
  const cursor = useRef<maplibregl.Marker | null>(null);
  const fittedFor = useRef<string | null>(null);
  const [basemapNote, setBasemapNote] = useState<string | null>(null);

  // What the once-registered handlers read. Refreshed after *every* render — no dependency
  // list — because the whole point is that a handler never holds a stale plan.
  const latest = useRef({ plan, drawMode, onPickSegment, onVertex });
  useEffect(() => {
    latest.current = { plan, drawMode, onPickSegment, onVertex };
  });

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
    popup.current = new maplibregl.Popup({ closeButton: false, closeOnMove: true });

    // Map-level rather than layer-scoped, and one handler rather than two. A layer click
    // and a map click both fire for the same press, so a draw mode registered separately
    // would place a vertex *and* select a segment; asking what was under the pointer is
    // the only version with one answer.
    const onClick = (event: maplibregl.MapMouseEvent) => {
      const current = latest.current;
      if (current.drawMode !== null) {
        current.onVertex({ lng: event.lngLat.lng, lat: event.lngLat.lat });
        return;
      }
      if (!instance.getLayer("segments")) return;
      const hit = instance.queryRenderedFeatures(event.point, { layers: ["segments"] })[0];
      if (!hit) return;
      const properties = hit.properties ?? {};
      current.onPickSegment({
        id: String(properties.id ?? ""),
        startM: Number(properties.start_m ?? 0),
        endM: Number(properties.end_m ?? 0),
      });
    };

    const onMove = (event: maplibregl.MapMouseEvent) => {
      if (latest.current.drawMode !== null || !instance.getLayer("segments")) {
        instance.getCanvas().style.cursor = latest.current.drawMode ? "crosshair" : "";
        popup.current?.remove();
        return;
      }
      const hit = instance.queryRenderedFeatures(event.point, { layers: ["segments"] })[0];
      if (!hit) {
        instance.getCanvas().style.cursor = "";
        popup.current?.remove();
        return;
      }
      instance.getCanvas().style.cursor = "pointer";
      popup.current
        ?.setLngLat(event.lngLat)
        .setText(String(hit.properties?.reason ?? ""))
        .addTo(instance);
    };

    instance.on("click", onClick);
    instance.on("mousemove", onMove);

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
      // Explicit, although `remove()` would take them with it. React 18's StrictMode runs
      // an effect twice in development, so a cleanup that leaves listeners behind makes
      // the second registration a silent duplicate — every gesture fired twice, which in
      // M12 means two vias for one click.
      instance.off("click", onClick);
      instance.off("mousemove", onMove);
      popup.current?.remove();
      cursor.current?.remove();
      map.current?.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !plan || plan.route.points.length < 2) return;

    const draw = () => {
      const data = { type: "FeatureCollection" as const, features: segmentFeatures(plan) };
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
          paint: { "line-color": ["get", "colour"], "line-width": ["get", "width"] },
        });
        instance.addSource("selection", { type: "geojson", data: emptyCollection() });
        instance.addLayer({
          id: "selection",
          type: "line",
          source: "selection",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#f2f6f9", "line-width": 3, "line-dasharray": [1, 1.4] },
        });
        instance.addSource("draft", { type: "geojson", data: emptyCollection() });
        instance.addLayer({
          id: "draft-fill",
          type: "fill",
          source: "draft",
          paint: { "fill-color": "#d1495b", "fill-opacity": 0.18 },
          filter: ["==", ["geometry-type"], "Polygon"],
        });
        instance.addLayer({
          id: "draft-line",
          type: "line",
          source: "draft",
          paint: { "line-color": "#d1495b", "line-width": 2 },
        });
      }

      // Once per plan **id**, never per plan object. See the module docstring: a write
      // round-trips through a job and a poll, so refitting here would throw away the pan
      // the user made the edit from.
      if (fittedFor.current !== plan.id) {
        const bounds = new maplibregl.LngLatBounds();
        for (const point of plan.route.points) bounds.extend([point.lon, point.lat]);
        instance.fitBounds(bounds, { padding: 48, duration: 0 });
        fittedFor.current = plan.id;
      }
    };

    if (instance.isStyleLoaded()) draw();
    else instance.once("load", draw);
  }, [plan]);

  useEffect(() => {
    const instance = map.current;
    const source = instance?.getSource("selection") as maplibregl.GeoJSONSource | undefined;
    if (!instance || !source || !plan) return;
    source.setData(selectionFeature(plan, selection));
  }, [plan, selection]);

  useEffect(() => {
    const instance = map.current;
    const source = instance?.getSource("draft") as maplibregl.GeoJSONSource | undefined;
    if (!instance || !source) return;
    source.setData(draftFeature(drawMode, vertices));
  }, [drawMode, vertices]);

  // M12.7: the timeline's cursor, as a place on the map. Nearest sampled point, which is
  // `edits.distance_along`'s rule — interpolating would be the UI drawing geometry the
  // backend never did.
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const point = plan && cursorM !== null ? pointAtDistance(plan.route.points, cursorM) : null;
    if (!point) {
      cursor.current?.remove();
      cursor.current = null;
      return;
    }
    if (!cursor.current) cursor.current = new maplibregl.Marker({ color: "#f2f6f9", scale: 0.7 });
    cursor.current.setLngLat([point.lon, point.lat]).addTo(instance);
  }, [plan, cursorM]);

  const gaps = plan ? plan.route.points.length - elevationRuns(plan.route.points).flat().length : 0;

  return (
    <div className="map">
      <div ref={container} className="map-canvas" />
      {!plan && <div className="map-empty">Plan a route, or pick one from the list.</div>}
      {plan && (
        <button
          className="map-fit"
          onClick={() => {
            const instance = map.current;
            if (!instance) return;
            const bounds = new maplibregl.LngLatBounds();
            for (const point of plan.route.points) bounds.extend([point.lon, point.lat]);
            instance.fitBounds(bounds, { padding: 48, duration: 0 });
          }}
        >
          Fit to route
        </button>
      )}
      {drawMode && (
        <div className="map-mode">
          {drawMode === "via" && "Click where the route should pass through."}
          {drawMode === "avoid" && `Click to outline an area to stay out of (${vertices.length}).`}
          {drawMode === "alternative" &&
            `Click along the line you want instead (${vertices.length}).`}
        </div>
      )}
      {basemapNote && <div className="map-note">{basemapNote}</div>}
      {gaps > 0 && (
        <div className="map-note">
          {gaps} point(s) on this line have no elevation reading — the stretch an edit
          replaced.
        </div>
      )}
    </div>
  );
}
