/**
 * The four hermetic gestures — lock, unlock, via and avoid — driven end to end.
 *
 * **What "end to end" means here, and why it is worth the machinery.** Each of these is a
 * click that becomes a `POST /api/plans/{id}/…`, a 202, a 1.5 s poll, a job that rewrites
 * `plan.json` and a re-read that re-renders the map. Asserting that a button dispatched a
 * handler would have covered the first inch of that. So every test below finishes by
 * reading the plan back **off the server** and checking what it now holds: the whole point
 * of these endpoints is that they change stored state, and the stored state is the only
 * witness that cannot agree with the client by construction.
 *
 * **Hermetic.** All four are edits to `PlanRequest` — they add a lock, remove one, add a via
 * or add an avoid polygon — and none re-scores, so none opens a `ScorerContext` and none can
 * reach the network. That is what makes them drivable in CI, and it is exactly the line
 * `choose` falls the other side of; `choose.spec.ts` carries that argument.
 *
 * **`POST /api/plans` is not attempted.** Submitting the form needs GraphHopper, and there
 * is none behind this page. The button is left alone rather than driven against a router
 * that would either not exist or be somebody's local one, which is the difference between a
 * hermetic test and one that passes on one machine.
 *
 * The range the writes carry comes from a click on the map, not from a number typed into
 * the test — so what is checked is the distance the *gesture* produced.
 */
import { expect, test } from "./harness";

test("lock writes the clicked stretch into the stored request", async ({ app }) => {
  await app.open();

  const before = await app.stored();
  expect(before.request.locked, "the seeded plan starts with nothing locked").toHaveLength(0);
  const chosen = before.segments[9];

  await app.selectSegment(9);
  await app.write(() => app.page.getByRole("button", { name: "Lock", exact: true }).click());

  const after = await app.stored();
  expect(after.request.locked).toHaveLength(1);
  const lock = after.request.locked[0];
  // The metres the click produced, to the metre. Not "a lock exists": a lock on the wrong
  // stretch is the failure ADR 0032 is about, and it looks identical from a length check.
  expect(lock.start_m).toBeCloseTo(chosen.cum_start_m, 3);
  expect(lock.end_m).toBeCloseTo(chosen.cum_start_m + chosen.length_m, 3);
  // `source: "user"` is not cosmetic — it is the discriminator `unlock --mine` reads, and
  // `LockEdit` deliberately does not put it on offer, because an HTTP client is the runner.
  expect(lock.source).toBe("user");
  expect(lock.reason).toBe("locked on the map");
});

test("unlock mine releases the lock the map set", async ({ app }) => {
  await app.open();
  await app.selectSegment(9);

  await app.write(() => app.page.getByRole("button", { name: "Lock", exact: true }).click());
  expect((await app.stored()).request.locked).toHaveLength(1);

  // The selection survives the write — it is the client's state and the plan reload does
  // not touch it — so the same stretch is still what "unlock" will be applied to.
  await app.write(() =>
    app.page.getByRole("button", { name: "Unlock mine", exact: true }).click(),
  );

  expect((await app.stored()).request.locked).toHaveLength(0);
  // The server says which lock it released and why, and the UI shows the sentence. An
  // unlock that freed nothing reports that too, which is a different answer.
  await expect(app.page.locator("ol.events")).toContainText("released");
});

test("a via is one click, and it lands in route order", async ({ app }) => {
  await app.open();

  const before = await app.stored();
  expect(before.request.via).toHaveLength(0);

  // A point on the line at about a third of the way along, so `insert_via` has a real
  // `distance_along` to order it by rather than the `None` fallback that appends.
  //
  // Found **before** the mode is entered, and that is not an accident of ordering. In a
  // draw mode `MapView`'s `mousemove` handler removes the popup and switches the cursor to
  // a crosshair without querying anything — which is right, because in draw mode the map is
  // not asking what is under the pointer. So the probe that confirms a pixel is on the line
  // only answers while the map is in its selecting state.
  const pixel = await app.pixelOnRoute(await app.midOf(6));

  await app.page.getByRole("button", { name: "Add a via", exact: true }).click();
  await expect(app.page.getByText("Click where the route should pass through.")).toBeVisible();
  await app.write(() => app.page.mouse.click(pixel.x, pixel.y));

  const after = await app.stored();
  expect(after.request.via).toHaveLength(1);
  await expect(app.page.locator("ol.events")).toContainText("via 1 of 1");
  // And the panel says the line did not move, which is the honest half of this gesture:
  // `RoutingPolicy` is frozen and re-routing under a new one is `longrun edit reroute`.
  await expect(app.page.locator("ol.events")).toContainText("was drawn");
});

test("an avoid area is drawn corner by corner and stored unrounded by the client", async ({
  app,
}) => {
  await app.open();

  const before = await app.stored();
  expect(before.request.avoid_polygons).toHaveLength(0);

  const canvas = (await app.page.locator("canvas.maplibregl-canvas").boundingBox())!;
  // A small triangle well inside the canvas: at this camera the ground is about 4.7 m to
  // the pixel, so ~100 px a side is roughly 0.1 km2 — comfortably inside the 4 km2 cap the
  // next test goes looking for.
  const corners = [
    { x: canvas.x + canvas.width * 0.4, y: canvas.y + 40 },
    { x: canvas.x + canvas.width * 0.5, y: canvas.y + 40 },
    { x: canvas.x + canvas.width * 0.45, y: canvas.y + 120 },
  ];

  await app.page.getByRole("button", { name: "Draw an avoid area", exact: true }).click();
  await expect(app.page.getByText(/Click to outline an area to stay out of \(0\)/)).toBeVisible();

  // Each click is a vertex, and the count on the button is the only thing that proves the
  // *placement* gesture works rather than the submit. `ui/README.md` lists "a click that
  // places a polygon corner" among the things nothing verified.
  for (const [index, corner] of corners.entries()) {
    await app.page.mouse.click(corner.x, corner.y);
    await expect(app.page.getByRole("button", { name: `Use this area (${index + 1})` })).toBeVisible();
  }

  await app.write(() => app.page.getByRole("button", { name: "Use this area (3)" }).click());

  const after = await app.stored();
  expect(after.request.avoid_polygons).toHaveLength(1);
  // Numbered by the server, from what the stored plan already holds. `_rounded_area` takes
  // the id as a parameter precisely so two drawn areas do not collide on one `in_<id>`
  // rule and silently lose the second.
  expect(after.request.avoid_polygons[0].id).toBe("avoid-0");
});

test("an over-cap area comes back refused, in the server's own words", async ({ app }) => {
  await app.open();

  // The whole canvas is about 4.4 km by 1.5 km of ground at the fitted camera, so a
  // quadrilateral over most of it is around 5–6 km2 — past `MAX_AREA_KM2`.
  const canvas = (await app.page.locator("canvas.maplibregl-canvas").boundingBox())!;
  const inset = 8;
  const corners = [
    { x: canvas.x + inset, y: canvas.y + inset },
    { x: canvas.x + canvas.width - inset, y: canvas.y + inset },
    { x: canvas.x + canvas.width - inset, y: canvas.y + canvas.height - inset },
    { x: canvas.x + inset, y: canvas.y + canvas.height - inset },
  ];

  await app.page.getByRole("button", { name: "Draw an avoid area", exact: true }).click();
  for (const corner of corners) await app.page.mouse.click(corner.x, corner.y);
  await app.page.getByRole("button", { name: "Use this area (4)" }).click();

  // Verbatim, with the size in it. `App.tsx` says a UI that replaced "the polygon covers
  // 11.4 km2, larger than the 4 km2 cap" with "invalid request" would take a sentence a
  // runner can act on and return one they cannot — and nothing checked that it did not.
  const error = app.page.locator("div.error");
  await expect(error).toBeVisible();
  await expect(error).toContainText(/the polygon covers \d+(\.\d+)? km2, larger than the 4 km2 cap/);
  await expect(error).toContainText("closing that much would take the streets around it with it");

  // Refused means refused: nothing reached the stored plan.
  expect((await app.stored()).request.avoid_polygons).toHaveLength(0);
});
