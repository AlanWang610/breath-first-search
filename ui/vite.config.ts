/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies to `longrun api`, so the dev server and the built bundle
// address the API identically at /api. Without this, development would use absolute
// URLs that production does not, which is the classic way a UI works until it is built.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
  // M12.8. `node`, not `jsdom`: everything under test here is a function of a plan and
  // returns a value, and a DOM would invite tests of components that assert they rendered
  // and look like gesture coverage without being any.
  //
  // **M15 kept the setting and corrected the reason.** The original comment went on to say
  // a headless run could not honestly check a MapLibre drag anyway. It can — in a browser,
  // which is what `ui/gestures/` is, and it found two bugs in this directory in its first
  // hour. What a *fake* DOM cannot do is answer `queryRenderedFeatures`, because jsdom and
  // happy-dom have no WebGL, so a test written against one would be asserting that a
  // handler called a mock. So this stays `node` and stays `.test.ts`, the browser tier is
  // a separate suite with a separate runner and a separate CI job, and neither can be
  // mistaken for the other. See ADR 0040.
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
