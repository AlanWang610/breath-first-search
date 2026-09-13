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
});
