/**
 * The Judge panel, driven end to end (ADR 0048).
 *
 * Every other gesture changes a plan; this one records something about a person, into the
 * suite's own ledger (`LABELS_DIR`, never `~/.longrun/labels`). So the witness is the same
 * as `edits.spec.ts`'s — the server's state, read back after the click — but it is the
 * ledger's answer through `GET /api/plans/{id}/measured`, not the plan: a verdict changes no
 * plan, and a test that looked for one would pass on a panel that recorded nothing.
 *
 * Every seeded plan is a byte-for-byte copy of one scored `synthetic-hazards`, and "judged"
 * is keyed on bytes, so verdicts from earlier tests in the run show up here too. Each test
 * counts from what it found rather than from zero.
 *
 * The seeded route has four LTS 4 segments, which is what the bounds below are about.
 */
import { expect, test } from "./harness";
import type { App } from "./harness";

const REASON =
  "Four LTS 4 crossings in four kilometres is more arterial than I would put in a long run.";

async function judged(app: App): Promise<{ id: string; would_run: string; via: string }[]> {
  const response = await app.page.request.get(`/api/plans/${app.planId}/measured`);
  expect(response.status()).toBe(200);
  return (await response.json()).judged;
}

async function fill(app: App, limit: string): Promise<void> {
  const panel = app.page.locator("section.judge");
  await panel.getByRole("radio", { name: "no" }).check();
  await panel.getByLabel("Why", { exact: true }).fill(REASON);
  await panel.getByRole("button", { name: "Add a bound" }).click();
  await panel.getByLabel("bound 1 metric").selectOption("lts4_count");
  await panel.getByLabel("bound 1 side").selectOption("max");
  await panel.getByLabel("bound 1 limit").fill(limit);
}

test("the sheet link is the stored plan's own sheet", async ({ app }) => {
  await app.open();

  const link = app.page.getByRole("link", { name: "Read the plan sheet" });
  await expect(link).toHaveAttribute("href", `/api/plans/${app.planId}/sheet`);
  const sheet = await app.page.request.get(`/api/plans/${app.planId}/sheet`);
  expect(sheet.status()).toBe(200);
  expect(await sheet.text()).toContain("<!doctype html>");
});

test("a limit starts empty, with this route's value beside it", async ({ app }) => {
  // The claim the panel's docstring makes and ADR 0022 is about: a limit pre-filled from the
  // plan's own number makes a `no` breach itself by construction.
  await app.open();
  const panel = app.page.locator("section.judge");

  await panel.getByRole("button", { name: "Add a bound" }).click();
  await panel.getByLabel("bound 1 metric").selectOption("lts4_count");

  await expect(panel.getByLabel("bound 1 limit")).toHaveValue("");
  await expect(panel.locator(".bound")).toContainText("this route: 4");
});

test("a verdict is recorded from the panel, as the person's, from the page", async ({ app }) => {
  await app.open();
  const before = await judged(app);

  await fill(app, "2");
  await app.page.getByRole("button", { name: "Record verdict" }).click();

  await expect(app.page.getByRole("status")).toContainText("Recorded verdict");
  const after = await judged(app);
  expect(after).toHaveLength(before.length + 1);
  const recorded = after.find((entry) => !before.some((old) => old.id === entry.id));
  expect(recorded?.would_run).toBe("no");
  expect(recorded?.via).toBe("ui");
});

test("a refusal is the server's own sentence, and nothing is recorded", async ({ app }) => {
  await app.open();
  const before = await judged(app);

  // At most 10 LTS 4 segments holds for a route with 4, so this `no` describes a route the
  // person would run — the property-1 mistake, caught while the route is on the screen.
  await fill(app, "10");
  await app.page.getByRole("button", { name: "Record verdict" }).click();

  await expect(app.page.getByLabel("Not recorded")).toContainText(
    "the envelope you would accept",
  );
  expect(await judged(app)).toHaveLength(before.length);
});
