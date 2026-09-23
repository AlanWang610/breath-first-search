/**
 * The stored plan every gesture is driven against, generated at test time and never
 * committed.
 *
 * **Why it is not a fixture.** `.gitignore` says it plainly: "a stored plan is a run of
 * somebody's, not a fixture: the goldens are where a plan this project tests against
 * belongs, and one committed here would be a sixth golden nobody reviewed." A plan checked
 * into `ui/` would be exactly that — a seventh route's worth of expectations that no golden
 * test reads, drifting from the schema the moment a scorer adds a key, and reviewed by
 * nobody because it arrived in a UI milestone.
 *
 * So it is produced the way the golden suite produces one: `longrun repair` over
 * `tests/golden/routes/synthetic-hazards`, with `LONGRUN_OFFLINE=1` and every input pinned
 * in the route directory. `tests/golden/harness.py` owns that argv and this mirrors it
 * exactly, down to `--target-km` and `--utc-offset`, so a change to how a golden is entered
 * shows up here as a failure rather than as a quiet divergence.
 *
 * **`synthetic-hazards` and not one of the six real routes**, for two reasons: it is 143 KB
 * against 5–15 MB, and it is the one route whose fixtures were written rather than
 * downloaded, so there is no licence question in reading it twice. It produces 19 segments,
 * 17 residual flags across seven scorers and a 3.95 km line — enough flagged ground that a
 * hover has prose to show and a click has a tier colour to land on.
 *
 * **The cassette is copied, never read in place.** `LONGRUN_CACHE_DIR` points at a cache
 * SQLite the server may open read-write, and the golden's `cache.sqlite` is a committed
 * fixture whose bytes are part of six routes' reproducibility. A suite that opened it
 * directly would be one power-cut away from moving a golden expectation, which is the one
 * thing M15 may not do.
 *
 * **One generation, many copies.** Every gesture writes to `plans/` on disk — a lock
 * rewrites `plan.json`, a via rewrites the request — so each test gets its own plan id
 * copied fresh from the seed. Tests would otherwise pass or fail on the order they ran in,
 * which is the failure the `A CORRECTION` commit on `main` is about: a test that agreed
 * with whoever ran it last.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PLANS_DIR, WORK_DIR } from "./playwright.config";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** The repository root, from `ui/gestures/`. */
export const REPO_ROOT = path.resolve(HERE, "..", "..");

/** The golden route the seed plan is scored from. */
export const GOLDEN = path.join(REPO_ROOT, "tests", "golden", "routes", "synthetic-hazards");

/** Where `longrun repair` writes, and where every test's copy is taken from. */
export const SEED_DIR = path.join(WORK_DIR, "seed");
export const SEED_PLAN = path.join(SEED_DIR, "plan.json");

/** The working copy of the golden's cassette, which is what the server is pointed at. */
export const CACHE_COPY = path.join(WORK_DIR, "cache.sqlite");

/**
 * Generate the seed plan, once per run.
 *
 * Playwright starts `webServer` *before* `globalSetup` — `createGlobalSetupTasks` puts the
 * plugin tasks first — so this runs against a live server and must not remove anything the
 * server owns. It prunes stored plans from earlier runs and leaves `plans/jobs` alone.
 */
export default async function globalSetup(): Promise<void> {
  if (!fs.existsSync(path.join(REPO_ROOT, "ui", "dist", "index.html"))) {
    // Said here rather than discovered as eight failing gestures. `longrun api --ui` warns
    // and then serves the API with no pages, so without this the first symptom is a blank
    // document and a timeout.
    throw new Error("ui/dist is not built; `npm run test:gestures` runs `npm run build` first");
  }

  fs.mkdirSync(PLANS_DIR, { recursive: true });
  for (const entry of fs.readdirSync(PLANS_DIR)) {
    if (entry === "jobs") continue;
    fs.rmSync(path.join(PLANS_DIR, entry), { recursive: true, force: true });
  }

  fs.rmSync(SEED_DIR, { recursive: true, force: true });
  fs.mkdirSync(SEED_DIR, { recursive: true });
  fs.copyFileSync(path.join(GOLDEN, "cache.sqlite"), CACHE_COPY);

  // `uv run`, not `.venv/Scripts/python.exe`: the entry point has to be on PATH, which is
  // the same reason `test_installed_entry_point_works` fails under a bare interpreter.
  execFileSync(
    "uv",
    [
      "run",
      "longrun",
      "repair",
      path.join(GOLDEN, "route.gpx"),
      "--date",
      "2026-09-12",
      "--start",
      "07:00",
      "--fixtures",
      path.join(GOLDEN, "fixtures"),
      "--profile",
      path.join(GOLDEN, "profile.yaml"),
      "--snapshot",
      path.join(GOLDEN, "snapshot.json"),
      "--cache",
      path.join(GOLDEN, "cache.sqlite"),
      "--target-km",
      "4.0",
      "--utc-offset",
      "-7",
      "--out",
      SEED_DIR,
    ],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, LONGRUN_OFFLINE: "1" },
      stdio: "pipe",
      shell: process.platform === "win32",
    },
  );

  const plan = JSON.parse(fs.readFileSync(SEED_PLAN, "utf-8"));
  // Checked rather than assumed. A `repair` that produced a plan with no segments would
  // otherwise surface as "no feature under the pointer", which reads as a WebGL problem.
  if (!Array.isArray(plan.segments) || plan.segments.length < 10) {
    throw new Error(`the seeded plan has ${plan.segments?.length ?? 0} segments; expected 19`);
  }
}

/**
 * One test's own copy of the seed, under its own plan id.
 *
 * The id is what `_plan_id_dir` will resolve, so it carries no separator, no colon and no
 * leading dot — the characters ADR 0036's resolver refuses. A test that needed one of those
 * would be testing the resolver, and `test_the_resolver_refuses_every_id_that_leaves_the_
 * plans_directory` already does that against the resolver itself.
 */
export function seedPlan(planId: string): string {
  const directory = path.join(PLANS_DIR, planId);
  fs.rmSync(directory, { recursive: true, force: true });
  fs.mkdirSync(directory, { recursive: true });
  fs.copyFileSync(SEED_PLAN, path.join(directory, "plan.json"));
  return planId;
}
