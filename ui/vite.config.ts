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
  // M12.8. `node`, not `jsdom`: everything under test is a function of a plan and returns a
  // value, and a DOM would invite tests of components that a headless run cannot honestly
  // check anyway. A MapLibre drag has no coverage here and the PR says so rather than
  // implying otherwise — mounting a component to assert it rendered would have looked like
  // it did.
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
