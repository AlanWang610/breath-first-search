/**
 * The browser tier (M15), which is a second suite and not a replacement for the first.
 *
 * `vite.config.ts` keeps `environment: "node"` and `include: ["src/**\/*.test.ts"]`, and
 * M12.8 chose that deliberately: a mounted component asserting that it rendered would have
 * raised the number without covering a single gesture, so the node tier was restricted to
 * functions of a plan that return a value. Nothing here changes that. `npm test` still runs
 * exactly the twenty cases it ran before; this config is reached only by
 * `npm run test:gestures`, and it runs in its own CI job for the reason M6 gives about the
 * `test` matrix's budget.
 *
 * **Chromium only, and headless.** MapLibre GL 5.x needs WebGL 2, and what a headless
 * browser gives it is a software rasteriser. Chromium's is ANGLE over SwiftShader and it is
 * the one this project measured (see `webgl.spec.ts`); Firefox and WebKit would be three
 * rasterisers to keep honest instead of one, for gestures whose behaviour is the
 * application's rather than the browser's.
 *
 * **`retries: 0`, on purpose.** A retry on a suite like this is how a flaky browser job
 * becomes one everybody learns to re-run, and a green tick nobody believes is worse than an
 * honest note in `ui/README.md` saying the gestures are hand-driven. If this suite ever
 * needs a retry to pass, that is the finding, and it belongs in the PR rather than in this
 * file.
 */
import { defineConfig, devices } from "@playwright/test";
import os from "node:os";
import path from "node:path";

/**
 * Everything the suite writes, outside the repository.
 *
 * `plans/` is gitignored with an explicit rationale — "a stored plan is a run of
 * somebody's, not a fixture" — so the seeded plan this suite drives is generated at test
 * time and lives here, never in the tree. `seed.ts` fills it.
 */
export const WORK_DIR = path.join(os.tmpdir(), "longrun-gestures");

/** Where the server reads stored plans from. One per run, many plan ids inside it. */
export const PLANS_DIR = path.join(WORK_DIR, "plans");

export const PORT = 8123;
export const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: ".",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    // Large enough that `fitBounds` puts the whole 4 km route on screen with room to
    // click a segment away from the endpoints, and fixed so a click at a projected pixel
    // means the same thing on every machine.
    viewport: { width: 1280, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
