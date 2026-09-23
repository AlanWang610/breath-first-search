/**
 * The timeline scrub, asserted rather than merely used.
 *
 * Every other file here leans on it — `cursorPixel` scrubs the strip and reads the marker
 * because that is how a test finds out where the map is pointing — but leaning on something
 * is not checking it. If the scrub silently stopped working, those tests would fail with
 * "no marker" and the cause would look like the map. So the gesture gets its own cases.
 *
 * It is M12.7's, and the reason it exists is in `Timeline.tsx`: the strip "was a pure
 * function of `plan` with no handlers and no state shared with `MapView`, so 'where on the
 * ground is this climb' had no answer short of counting kilometres by eye". Two siblings
 * cannot share state without their parent holding it, so the distance lives in `App` and
 * both components read it — which means this is also the only test here that crosses two
 * components through their parent.
 *
 * **The marker is the nearest sampled point, not an interpolation.** `pointAtDistance`
 * follows `core.plan.edits.distance_along`'s rule deliberately: interpolating would be the
 * UI drawing geometry the backend never did. So the marker advances in steps as the cursor
 * slides, and the test asserts a monotone advance rather than a proportional one.
 */
import { expect, test } from "./harness";

test("scrubbing names a distance and puts it on the map", async ({ app }) => {
  await app.open();

  const header = app.page.locator("section.timeline header");
  await expect(header).not.toContainText("cursor at");
  await expect(app.page.locator(".maplibregl-marker")).toHaveCount(0);

  const total = (await app.stored()).route.points.slice(-1)[0].cum_dist_m;
  const strip = (await app.page.locator("section.timeline svg").boundingBox())!;
  await app.page.mouse.move(strip.x + strip.width * 0.5, strip.y + strip.height / 2);

  // Half way along a 3.95 km line, printed to the same two decimals `App.tsx` prints.
  await expect(header).toContainText(`cursor at ${((total * 0.5) / 1000).toFixed(2)} km`);
  await expect(app.page.locator(".maplibregl-marker")).toHaveCount(1);
  // And drawn on the strip itself, so the two ends of the gesture agree.
  await expect(app.page.locator("section.timeline svg line")).toHaveCount(1);
});

test("the marker walks the route as the cursor slides along it", async ({ app }) => {
  await app.open();

  const seen: number[] = [];
  for (const fraction of [0.15, 0.35, 0.55, 0.75]) {
    const total = (await app.stored()).route.points.slice(-1)[0].cum_dist_m;
    seen.push((await app.cursorPixel(total * fraction)).x);
  }

  // Strictly increasing: the seeded route runs west to east, so further along is further
  // right. A marker that did not move, or moved once and stuck, is the failure this
  // catches — and it is exactly what a stale `cursorM` would look like.
  for (let index = 1; index < seen.length; index += 1) {
    expect(seen[index], `step ${index} did not advance`).toBeGreaterThan(seen[index - 1] + 10);
  }
});

test("leaving the strip takes the cursor with it", async ({ app }) => {
  await app.open();

  const strip = (await app.page.locator("section.timeline svg").boundingBox())!;
  await app.page.mouse.move(strip.x + strip.width * 0.5, strip.y + strip.height / 2);
  await expect(app.page.locator(".maplibregl-marker")).toHaveCount(1);

  // `onMouseLeave` sets the distance back to `null`, and `null` is not zero — zero is the
  // start of the route, and a marker parked at the start would be a position nobody asked
  // for. Absence rendered as absence, in the one place on this page where it is a gesture.
  await app.page.mouse.move(strip.x + strip.width * 0.5, strip.y - 40);
  await expect(app.page.locator(".maplibregl-marker")).toHaveCount(0);
  await expect(app.page.locator("section.timeline header")).not.toContainText("cursor at");
});

test("the note about what is not drawn is absent because nothing is missing", async ({ app }) => {
  await app.open();

  // Scope 3.6 at the last surface it can be lost at: `Timeline.tsx` names the series it
  // cannot draw rather than leaving a reader to assume the route has no heat on it. The
  // thing worth pinning is that the note is *conditional* — a strip that always printed it
  // would be as uninformative as one that never did.
  //
  // So the expectation is derived from the plan rather than typed in. This one was scored
  // by every scorer and carries an ETA per point, which is exactly the case where there is
  // nothing to name; `choose.spec.ts` covers the other side, where a spliced stretch has no
  // elevation and the strip says how much in kilometres.
  const plan = await app.stored();
  const scorers = new Set(plan.results.map((result) => result.name));
  for (const needed of ["heat_stress", "sun_exposure", "resupply_schedule", "lighting"]) {
    expect(scorers, `the seeded plan should carry ${needed}`).toContain(needed);
  }
  expect(plan.route.points.every((point) => point.ele_m !== null)).toBe(true);

  await expect(app.page.locator("section.timeline")).not.toContainText("Not drawn, because");
  // One unbroken polyline, because every point on this line was measured. A second one
  // would mean a gap, and a gap here would mean the plan is not what it says it is.
  await expect(app.page.locator("section.timeline svg polyline")).toHaveCount(1);
});
