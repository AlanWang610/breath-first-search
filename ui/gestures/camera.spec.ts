/**
 * The one M12's own docstring says is untested: that a write does not throw away the pan
 * the user made the edit from.
 *
 * M12.4 exists for this. `MapView`'s module docstring names the bug and the fix:
 *
 * > *`fitBounds` refit on every plan change.* Which is correct when somebody opens a
 * > different plan and destroys their work when the same plan comes back from a write — an
 * > edit round-trips through a job and a poll, so the first lock a user set would have
 * > thrown away the pan they set it from. Fitted once per plan **id** now […] A camera the
 * > user moved is state, and a re-render is not a reason to discard state.
 *
 * `fittedFor.current !== plan.id` is three tokens, it is the entire guard, and until now
 * nothing anywhere checked it. It is also the single hardest thing in this directory to
 * check by hand, because the symptom is a camera snapping back roughly a second and a half
 * after a click, and the way to see it is to have panned far enough to notice.
 *
 * **How the camera is observed without a handle on the map.** The same way every other test
 * here finds a pixel: scrub the timeline to a fixed distance and read where the cursor
 * marker lands. That pixel is `map.project()` of a route point, so if the camera has not
 * moved the same distance projects to the same pixel, and if `fitBounds` re-ran the pixel
 * goes back to where it was before the pan. The assertion is therefore on the *difference*,
 * which is what a user would see.
 *
 * **The pan is asserted too, before the write.** MapLibre's drag has inertia, and a drag
 * that coasted would leave the camera somewhere nobody chose — making the test weaker
 * without saying so. `panBy` pauses before releasing so the inertia sampler sees no recent
 * movement, and this checks that the pan landed where it was aimed before it trusts the
 * rest.
 *
 * **And the write must be a real one.** The whole point is the re-render: the poll sees a
 * terminal status, calls `openPlan`, a new `Plan` object arrives, and the draw effect runs
 * again on a plan whose `id` has not changed. `write()` waits for exactly that response and
 * then settles, so the measurement below is taken after the moment the old code would have
 * refitted.
 */
import { App, expect, test } from "./harness";
import { seedPlan } from "./seed";

/** How far the map is dragged. Large enough that a refit is unmistakable at this camera. */
const PAN_X = 220;
const PAN_Y = 90;

/** Slack for the projection: sub-pixel rounding and MapLibre's own transform arithmetic. */
const TOLERANCE_PX = 4;

test("a lock does not throw away the pan the user made it from", async ({ app }) => {
  await app.open();

  // A fixed distance, measured three times: before the pan, after it, and after a write.
  const probeM = await app.midOf(9);

  await app.selectSegment(9);
  const fitted = await app.cursorPixel(probeM);

  await app.panBy(PAN_X, PAN_Y);
  const panned = await app.cursorPixel(probeM);
  const drift = { x: panned.x - fitted.x, y: panned.y - fitted.y };
  expect(Math.abs(drift.x - PAN_X), "the drag coasted or fell short").toBeLessThanOrEqual(
    TOLERANCE_PX,
  );
  expect(Math.abs(drift.y - PAN_Y), "the drag coasted or fell short").toBeLessThanOrEqual(
    TOLERANCE_PX,
  );

  await app.write(() => app.page.getByRole("button", { name: "Lock", exact: true }).click());

  // The write landed — otherwise this test would pass on a page where nothing happened.
  expect((await app.stored()).request.locked).toHaveLength(1);

  const after = await app.cursorPixel(probeM);
  expect(Math.abs(after.x - panned.x), "the camera moved across a write").toBeLessThanOrEqual(
    TOLERANCE_PX,
  );
  expect(Math.abs(after.y - panned.y), "the camera moved across a write").toBeLessThanOrEqual(
    TOLERANCE_PX,
  );
  // Said the other way round as well, because "close to where it was" and "not back where
  // it started" are different claims and only the second one is the bug.
  expect(Math.abs(after.x - fitted.x)).toBeGreaterThan(PAN_X / 2);
});

test("Fit to route is how the camera comes back, and it is the user's choice", async ({ app }) => {
  await app.open();

  const probeM = await app.midOf(9);
  const fitted = await app.cursorPixel(probeM);

  await app.panBy(PAN_X, PAN_Y);
  expect((await app.cursorPixel(probeM)).x - fitted.x).toBeGreaterThan(PAN_X / 2);

  // The other half of M12.4's decision: the refit did not disappear, it became a button.
  // A guard that kept the camera and gave no way back would have traded one lost state for
  // another.
  await app.page.getByRole("button", { name: "Fit to route", exact: true }).click();
  await app.page.waitForTimeout(300);

  const refitted = await app.cursorPixel(probeM);
  expect(Math.abs(refitted.x - fitted.x)).toBeLessThanOrEqual(TOLERANCE_PX);
  expect(Math.abs(refitted.y - fitted.y)).toBeLessThanOrEqual(TOLERANCE_PX);
});

test("opening a different plan does fit to it", async ({ app }) => {
  // The other half of the guard, and the half that makes it a guard rather than a switch:
  // `fittedFor.current !== plan.id` must still be true for a plan that is not this one, or
  // the fix for one bug is a second bug — a camera left pointing at somebody else's route.
  //
  // It takes a second *route*, not a second stored copy. `plan.id` is derived from the
  // route, so every copy of one seed carries the same one and switching between copies
  // looks, to this guard, like the same plan. `seed.ts` scores `kc-stateline` for exactly
  // this test and asserts the two ids differ.
  const other = new App(app.page, seedPlan(`${app.planId}-kc`, "kc-stateline"));

  await other.open();
  const fitted = await other.cursorPixel(await other.midOf(20));

  await app.switchTo();
  await app.panBy(PAN_X, PAN_Y);

  await other.switchTo();
  const refitted = await other.cursorPixel(await other.midOf(20));
  expect(Math.abs(refitted.x - fitted.x)).toBeLessThanOrEqual(TOLERANCE_PX);
  expect(Math.abs(refitted.y - fitted.y)).toBeLessThanOrEqual(TOLERANCE_PX);
});
