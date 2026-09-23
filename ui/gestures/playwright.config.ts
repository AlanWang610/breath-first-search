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
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

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

/** The golden route the seeded plan is scored from; `seed.ts` says why this one. */
const GOLDEN = path.join(REPO_ROOT, "tests", "golden", "routes", "synthetic-hazards");

export default defineConfig({
  testDir: ".",
  // **One worker, no parallelism, and that is about the server rather than about speed.**
  // There is one `longrun api` process with one plans directory, and every gesture writes
  // to it. Tests take their own plan id so they cannot collide over a plan — but the job
  // runner is a shared thread pool, and two writes racing through it would make a poll
  // tick mean something different in each test. Sequential is what the suite asserts
  // about.
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  globalSetup: "./seed.ts",

  /**
   * One server, serving the API and the built pages from the same origin.
   *
   * That is `longrun api --ui ui/dist`, which is what `ui/README.md` tells a reviewer to
   * run and what the gestures were exercised by hand against. The alternative — vite's dev
   * server with its `/api` proxy — would test a two-origin arrangement that only exists
   * during development.
   *
   * Four environment variables, each load-bearing:
   *
   *   `LONGRUN_TILE_PROVIDER=none` because the default is `usgs-imagery` and the map would
   *   reach the National Map for basemap tiles on every run. That is a network dependency
   *   in a suite that must not have one, and a slow one. ADR 0023's design is what makes
   *   this safe: the basemap is a *layer*, so switching it off leaves the dark ground with
   *   the route still drawn on it, which is precisely the state the gestures need.
   *
   *   `LONGRUN_OFFLINE=1`, `LONGRUN_FIXTURES` and `LONGRUN_CACHE_DIR` because
   *   `_choose_alternative` re-scores through `ToolSettings.from_env()`. Without them a
   *   `choose` would score against `data/` — which does not exist — and any scorer that
   *   reached for a forecast would reach the network. With them it reads the golden's own
   *   fixtures and a *copy* of its cassette, which is why `seed.ts` copies rather than
   *   points. The other four gestures are request edits and need none of this; it is set
   *   for all of them anyway, because a server configured differently from the one a test
   *   reasons about is a bug waiting for the next milestone.
   *
   * `reuseExistingServer: false` everywhere, not only in CI. A server left over from an
   * earlier run has a different plans directory and a different environment, and a suite
   * that quietly attached to it would be testing something nobody could name.
   */
  webServer: {
    command: `uv run longrun api --host 127.0.0.1 --port ${PORT} --plans "${PLANS_DIR}" --ui ui/dist`,
    cwd: REPO_ROOT,
    url: `${BASE_URL}/api/health`,
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      LONGRUN_TILE_PROVIDER: "none",
      LONGRUN_OFFLINE: "1",
      LONGRUN_FIXTURES: path.join(GOLDEN, "fixtures"),
      LONGRUN_CACHE_DIR: WORK_DIR,
    },
  },

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // **After the device spread, not before it.** `devices["Desktop Chrome"]` carries a
        // viewport of its own, so a `viewport` set on the top-level `use` is overridden by
        // the project and silently has no effect — which is how a config can document a
        // window size it does not have. Set here, it is the one that applies.
        //
        // The size matters because `.map` is `55vh` with a `320px` floor: at 720 the map is
        // pinned to the floor, and a taller window gives `fitBounds` more canvas and every
        // gesture more room between the route and the edge. Fixed rather than inherited so
        // that a projected pixel means the same thing on every machine.
        viewport: { width: 1280, height: 900 },
      },
    },
  ],
});
