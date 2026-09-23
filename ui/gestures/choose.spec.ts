/**
 * `choose` — the fifth gesture, and the one that is not a request edit.
 *
 * **Why it is different from the other four.** Lock, unlock, via and avoid change
 * `PlanRequest` and stop. `_choose_alternative` splices a drawn line into the stored route,
 * auto-locks it (ADR 0019: the runner chose it) and then **re-scores**, through
 * `open_context` with a `ToolSettings.from_env()`. So it is the one gesture that opens a
 * scoring context from inside the API process, and the only one that can reach the network
 * if the environment is wrong.
 *
 * It is in this suite because the environment is not left wrong. `playwright.config.ts`
 * gives the server `LONGRUN_FIXTURES` pointing at the golden's own `fixtures/`,
 * `LONGRUN_CACHE_DIR` pointing at a **copy** of its cassette, and `LONGRUN_OFFLINE=1`, so a
 * scorer that reached for something not recorded fails as a loud cache miss rather than
 * quietly fetching it. That is the same arrangement `tests/golden/harness.py` runs a golden
 * under, and the same reason it uses it.
 *
 * **The elevation gap is the interesting assertion.** `splice` keeps the heights of the
 * ground it did not touch and leaves the replacement's `None`, deliberately — re-reading a
 * DEM for the untouched two thirds is M10.4's bug through another door. So a chosen
 * alternative is the one thing that produces a route whose *middle* is unmeasured, and
 * M12.7 rewrote the timeline to draw that as a break rather than as flat ground. Nothing
 * else in this suite can reach that state, and until now nothing verified it.
 */
import { expect, test } from "./harness";

test("a drawn alternative is spliced in, locked as the runner's, and re-scored", async ({
  app,
}) => {
  await app.open();

  const before = await app.stored();
  const target = before.segments[9];
  const beforeLength = before.route.points[before.route.points.length - 1].cum_dist_m;
  expect(before.route.points.every((point) => point.ele_m !== null)).toBe(true);

  // Three pixels for the replacement line, all found **before** the draw mode is entered:
  // in a draw mode the hover handler stops querying, so a pixel cannot be confirmed then.
  const start = await app.pixelOnRoute(target.cum_start_m + 15);
  const middle = await app.pixelOnRoute(target.cum_start_m + target.length_m / 2);
  const end = await app.pixelOnRoute(target.cum_start_m + target.length_m - 15);
  // Pulled off the line so the alternative is a real detour rather than the same ground
  // drawn again. About 40 px is roughly 190 m at this camera.
  const detour = { x: middle.x, y: middle.y - 40 };

  await app.selectSegment(9);
  await app.page.getByRole("button", { name: "Replace this stretch", exact: true }).click();
  await expect(app.page.getByText(/Click along the line you want instead \(0\)/)).toBeVisible();

  for (const [index, pixel] of [start, detour, end].entries()) {
    await app.page.mouse.click(pixel.x, pixel.y);
    await expect(app.page.getByRole("button", { name: `Use this line (${index + 1})` })).toBeVisible();
  }

  await app.write(() => app.page.getByRole("button", { name: "Use this line (3)" }).click());

  const after = await app.stored();

  // 1. The line moved. `splice` replaced the stretch, so the total length is not what it
  //    was — which is the only proof that geometry, and not just the request, changed.
  const afterLength = after.route.points[after.route.points.length - 1].cum_dist_m;
  expect(Math.abs(afterLength - beforeLength)).toBeGreaterThan(1);

  // 2. It locked itself, as the runner's own. ADR 0019: they chose it, so `unlock --mine`
  //    can take it back and the loop's next round will not reopen it.
  const chosen = after.request.locked.find((lock) => lock.reason === "chose an alternative");
  expect(chosen, "a chosen alternative locks itself").toBeDefined();
  expect(chosen!.source).toBe("user");

  // 3. The new stretch has no elevation, and the timeline says so in metres rather than
  //    drawing a straight line across it. This is M12.7's whole argument, reachable only
  //    through this gesture.
  expect(after.route.points.some((point) => point.ele_m === null)).toBe(true);
  await expect(app.page.locator("section.timeline")).toContainText(
    /km of this line has no elevation reading/,
  );
  // And drawn as a break: one polyline per run of measured points, so the unmeasured
  // middle is a hole in the chart. Filtering the nulls out and drawing through the rest —
  // which is what this did before M12.7 — puts a straight line across the gap, and a
  // straight line on an elevation chart reads as flat ground.
  await expect(app.page.locator("section.timeline svg polyline")).toHaveCount(2);
  // Said on the map as well, counted in points rather than in kilometres. Two different
  // notes, because "how much of this chart is missing" and "how many points on this line
  // are unmeasured" are different questions and the reader is in a different place.
  await expect(
    app.page.getByText(/point\(s\) on this line have no elevation reading/),
  ).toBeVisible();

  // 4. It re-scored rather than carrying the old measurements onto new ground, which is the
  //    state M11 exists to prevent — and the log counts how many it re-ran against how many
  //    it carried, which is the claim rather than a word for it. The panel shows the last
  //    eight events, so the earlier "re-scoring the edited line" line has scrolled off by
  //    the time the job completes; this is the one that is still on screen and it says more.
  const events = app.page.locator("ol.events");
  await expect(events).toContainText(/re-scored \d+, carried \d+/);
  await expect(events).toContainText(/residual flags: \d+ -> \d+/);
});
