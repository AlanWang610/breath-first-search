/**
 * The spike that had to come back clean before any of the rest of M15 was written, kept as
 * the suite's canary.
 *
 * **Why it exists at all.** MapLibre GL 5.x requires WebGL 2, and a headless browser has no
 * GPU: Chromium falls back to ANGLE over SwiftShader, a software rasteriser. Everything
 * else in this directory is built on the assumption that that fallback both *exists* and
 * *answers `queryRenderedFeatures` correctly* — a click on the map is turned into a
 * segment by `MapView`'s `queryRenderedFeatures(event.point, { layers: ["segments"] })`,
 * and that call reads the rendered tile, not the source data. A rasteriser that produced a
 * frame but resolved no features would make every gesture test fail in a way that looked
 * like an application bug.
 *
 * **What was measured before the suite was designed**, on Windows, Chromium 153 /
 * chromium-headless-shell, over 25 consecutive launches: 25 of 25 resolved exactly one
 * feature with the right properties. The first launch was 2.8 s and the rest were
 * 550–660 ms each; `map.once("load")` fired 24–44 ms after construction. Nothing flaked, so
 * the suite was built. Had it not, the honest answer was to stop and leave `ui/README.md`
 * saying the gestures are hand-driven.
 *
 * **It loads no application and needs no API.** That is the point of keeping it. When this
 * directory goes red, this file answers the first question — is it the browser or is it the
 * code — before anybody opens a trace. If this passes and the rest fails, the rasteriser is
 * fine and something in `ui/src` broke.
 *
 * `maplibre-gl.js` is injected from `node_modules` rather than imported, because an import
 * would need a bundler and a served page, and each of those is a way for this file to fail
 * for a reason that is not WebGL.
 */
import { expect, test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MAPLIBRE_JS = path.join(HERE, "..", "node_modules", "maplibre-gl", "dist", "maplibre-gl.js");
const MAPLIBRE_CSS = path.join(HERE, "..", "node_modules", "maplibre-gl", "dist", "maplibre-gl.css");

test.describe("headless WebGL", () => {
  test("the rasteriser reports WebGL 2, which is MapLibre 5's floor", async ({ page }) => {
    await page.setContent('<!doctype html><html><body style="margin:0"></body></html>');
    const report = await page.evaluate(() => {
      const gl = document.createElement("canvas").getContext("webgl2");
      if (!gl) return { webgl2: false, version: null as string | null };
      return { webgl2: true, version: String(gl.getParameter(gl.VERSION)) };
    });
    // Asserted rather than logged. A Chromium that quietly stopped shipping SwiftShader
    // would otherwise surface as eight failing gesture tests and no explanation.
    expect(report.webgl2).toBe(true);
    expect(report.version).toContain("WebGL 2.0");
  });

  test("a line layer resolves a click through queryRenderedFeatures", async ({ page }) => {
    await page.setContent(
      '<!doctype html><html><body style="margin:0">' +
        '<div id="map" style="width:900px;height:600px"></div></body></html>',
    );
    await page.addStyleTag({ path: MAPLIBRE_CSS });
    await page.addScriptTag({ path: MAPLIBRE_JS });

    const hit = await page.evaluate(async () => {
      // The same shape `MapView` builds: a blank inline style with no glyphs and no
      // network of any kind, one GeoJSON line source, one `line` layer named `segments`,
      // and the metres carried on the feature rather than looked up (M11.3).
      const maplibregl = (window as unknown as { maplibregl: typeof import("maplibre-gl") })
        .maplibregl;
      const map = new maplibregl.Map({
        container: "map",
        style: {
          version: 8,
          sources: {},
          layers: [{ id: "bg", type: "background", paint: { "background-color": "#11151a" } }],
        },
        center: [-122.42, 37.78],
        zoom: 12,
        attributionControl: false,
      });
      await new Promise<void>((resolve, reject) => {
        map.once("load", () => resolve());
        setTimeout(() => reject(new Error("the map never loaded")), 30_000);
      });

      const coordinates: [number, number][] = [];
      for (let i = 0; i < 40; i += 1) coordinates.push([-122.45 + i * 0.002, 37.76 + i * 0.001]);
      map.addSource("segments", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: [
            {
              type: "Feature",
              properties: { id: "s00007", start_m: 700, end_m: 800 },
              geometry: { type: "LineString", coordinates },
            },
          ],
        },
      });
      map.addLayer({
        id: "segments",
        type: "line",
        source: "segments",
        paint: { "line-color": "#d1495b", "line-width": 6 },
      });
      const bounds = new maplibregl.LngLatBounds();
      for (const coordinate of coordinates) bounds.extend(coordinate);
      map.fitBounds(bounds, { padding: 48, duration: 0 });
      await new Promise<void>((resolve) => map.once("idle", () => resolve()));

      const features = map.queryRenderedFeatures(map.project(coordinates[20]), {
        layers: ["segments"],
      });
      return {
        count: features.length,
        id: features[0]?.properties?.id ?? null,
        startM: features[0]?.properties?.start_m ?? null,
      };
    });

    expect(hit.count).toBe(1);
    expect(hit.id).toBe("s00007");
    // The properties and not only the hit: `MapView` reads `start_m`/`end_m` off the
    // feature and sends those metres to the server, so a query that returned a feature
    // with no properties would still break every write.
    expect(hit.startM).toBe(700);
  });
});
