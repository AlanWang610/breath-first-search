/**
 * The two gestures everything else is built on: a click that selects a stretch, and a hover
 * that says why a segment is coloured.
 *
 * `ui/README.md` has named both as uncovered since M12 — "a click that selects a segment, a
 * click that places a polygon corner, a drag on the MapLibre canvas, the popup on hover —
 * none of those have automated coverage, here or anywhere". These two have no write behind
 * them, so they are where the rendered frame itself is checked: a click that reaches
 * `queryRenderedFeatures` and comes back with a feature carrying `start_m` and `end_m`, and
 * a pointer that finds prose rather than a reason code.
 *
 * **The selection is asserted as metres, not only as an id.** The panel prints
 * `s000NN · a.aa–b.bb km`, and those kilometres are the range a lock will be applied to —
 * ADR 0032's point is that the id is positional and the distance is not. A test that only
 * checked the id would pass on a UI that sent the wrong stretch.
 */
import { expect, test } from "./harness";

test("a click on a segment selects that stretch, in metres", async ({ app }) => {
  await app.open();

  const plan = await app.stored();
  const chosen = plan.segments[9];
  await app.selectSegment(9);

  const panel = app.page.locator("section.panel p.small").first();
  await expect(panel).toContainText(chosen.id);
  // The same arithmetic `segmentRange` does and `App.tsx` prints.
  const startKm = (chosen.cum_start_m / 1000).toFixed(2);
  const endKm = ((chosen.cum_start_m + chosen.length_m) / 1000).toFixed(2);
  await expect(panel).toContainText(`${startKm}–${endKm} km`);
});

test("the buttons that need a selection are disabled until there is one", async ({ app }) => {
  await app.open();

  // Not decoration: the three range gestures read `selection.startM` and `selection.endM`,
  // and a live button with nothing selected is a range of `undefined` on the wire.
  const lock = app.page.getByRole("button", { name: "Lock", exact: true });
  const unlock = app.page.getByRole("button", { name: "Unlock mine", exact: true });
  const replace = app.page.getByRole("button", { name: "Replace this stretch", exact: true });

  await expect(lock).toBeDisabled();
  await expect(unlock).toBeDisabled();
  await expect(replace).toBeDisabled();
  await expect(app.page.getByText("Click a segment on the map to select a stretch.")).toBeVisible();

  await app.selectSegment(9);

  await expect(lock).toBeEnabled();
  await expect(unlock).toBeEnabled();
  await expect(replace).toBeEnabled();
});

test("hovering a flagged segment shows the reason in prose", async ({ app }) => {
  await app.open();

  const plan = await app.stored();
  const flagged = new Set<string>();
  for (const result of plan.results) for (const flag of result.flags) flagged.add(flag.segment_id);
  expect(flagged.size, "the seeded plan should carry flags to hover over").toBeGreaterThan(0);

  // Walk the line until a flagged segment is under the pointer. Which one it is depends on
  // what the scorers said, and pinning a particular id here would make this a golden.
  let reason: string | null = null;
  for (let index = 1; index < plan.segments.length - 1 && reason === null; index += 1) {
    const id = plan.segments[index].id;
    if (!flagged.has(id)) continue;
    const text = await app.hoverReason(await app.pixelOnRoute(await app.midOf(index)));
    if (text?.startsWith(`${id}:`)) reason = text;
  }

  expect(reason, "no flagged segment produced a popup").not.toBeNull();
  // The shape `MapView` builds and the reason it builds it: "a reason code alone tells a
  // developer something and a runner nothing". So the popup must name the scorer, the kind
  // and the tier **in words**, and then say something after the dash. `hazards 0 (2)` —
  // which is what this printed until M15.4 — passes a looser assertion and helps nobody.
  expect(reason).toMatch(/^s\d{5}: \w+ (soft|hard) \((safety|physiological|comfort)\) — \S/);
});

test("hovering bare ground shows nothing at all", async ({ app }) => {
  await app.open();

  // Absence rendered as absence, at the last surface it can be lost at. A popup that
  // lingered over empty ground would attribute a reason to a place that has none.
  const canvas = await app.page.locator("canvas.maplibregl-canvas").boundingBox();
  expect(canvas).not.toBeNull();
  expect(await app.hoverReason({ x: canvas!.x + 6, y: canvas!.y + 6 })).toBeNull();
});
