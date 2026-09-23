/**
 * The hand-written client types, checked against a plan the server actually produced.
 *
 * **This is the gap M15 found two bugs in.** `ui/src/api.ts` says so about itself: the types
 * are written by hand against the plan schema, and `tsc` "checks them against each other
 * rather than against the server". `TradeOff.options` type-checked for five milestones and
 * threw on every plan that had a trade-off. `Flag.kind` and `Flag.tier` were declared as
 * strings while every server that has ever run dumped `IntEnum` integers, so `tierColour`
 * fell through to its default for every flag on every map. `plan.test.ts` could not catch
 * either, because it builds its fixtures *from* those types and therefore agrees with
 * whatever they say.
 *
 * Generating the types from `/openapi.json` would close it, and `api.ts` argues against that
 * for a reason that still holds: a complete TypeScript mirror is a second schema, and it
 * would drift the first time a scorer added a key. The types are deliberately **partial**.
 *
 * So this is the other way to close it — not a generated mirror, but the few assertions
 * that say *this field, which the UI branches on, has this shape on the wire*. It is small
 * on purpose. A test that walked the whole response would be the second schema by another
 * route, and would fail on a key a scorer added that the UI does not draw.
 *
 * It lives in the browser tier because it needs a running server and a real plan, and the
 * node tier has neither and must not acquire either.
 */
import { expect, test } from "./harness";

test("a flag's kind and tier arrive as the integers the UI branches on", async ({ app }) => {
  const plan = await app.stored();
  const flags = plan.results.flatMap((result) => result.flags);
  expect(flags.length, "the seeded plan should carry flags").toBeGreaterThan(0);

  for (const flag of flags) {
    // `FlagKind` is `SOFT = 0, HARD = 1` and `Tier` is `SAFETY = 0, PHYSIOLOGICAL = 1,
    // COMFORT = 2`. If either ever became a string on the wire, `tierColour` and
    // `worst.kind === FLAG_KIND.HARD` would both go quiet again, in the same direction.
    expect(typeof flag.kind, `${flag.scorer}/${flag.reason_code} kind`).toBe("number");
    expect(typeof flag.tier, `${flag.scorer}/${flag.reason_code} tier`).toBe("number");
    expect([0, 1]).toContain(flag.kind);
    expect([0, 1, 2]).toContain(flag.tier);
  }
});

test("a trade-off carries option_a and option_b, not an options array", async ({ app }) => {
  // The bug that started this: `TradeOff` declared `options: string[]`, `trade.options.map`
  // type-checked, and the panel threw `Cannot read properties of undefined` on the first
  // plan that had one — taking every sibling panel down with it. The seeded plan has no
  // trade-offs, so what is pinned here is the *shape of the field*, which is what the
  // client reads before it maps anything.
  const response = await app.page.request.get(`/api/plans/${app.planId}`);
  const raw = (await response.json()) as { trade_offs: Record<string, unknown>[] };
  expect(Array.isArray(raw.trade_offs)).toBe(true);
  for (const trade of raw.trade_offs) {
    expect(trade).toHaveProperty("option_a");
    expect(trade).toHaveProperty("option_b");
    expect(trade).not.toHaveProperty("options");
  }
});

test("absence is still absence by the time it reaches the client", async ({ app }) => {
  // Scope 3.6 at the API boundary: `null` is "nobody measured" and the UI threads it to the
  // label rather than rendering `0`. What this pins is that the *key is present* and the
  // value is `null` — a field dropped from the response would read as `undefined` in the
  // client, and `undefined` renders as nothing at all rather than as "not measured".
  const response = await app.page.request.get(`/api/plans/${app.planId}`);
  const raw = (await response.json()) as {
    metrics: Record<string, unknown>;
    route: { points: Record<string, unknown>[] };
    coverage: { entries: Record<string, unknown>[] };
  };

  expect(raw.metrics).toHaveProperty("detour_ratio");
  expect(raw.route.points[0]).toHaveProperty("ele_m");
  expect(raw.coverage.entries.length).toBeGreaterThan(0);
  for (const entry of raw.coverage.entries) {
    // A coverage entry that was not checked is listed *with its reason*, never omitted.
    expect(entry).toHaveProperty("checked");
    if (entry.checked === false) expect(entry).toHaveProperty("reason");
  }
});

test("the basemap is off, and the map says so rather than going quietly blank", async ({ app }) => {
  // Two things at once. The suite needs `LONGRUN_TILE_PROVIDER=none` for determinism, and
  // ADR 0023's reason for making the basemap a *layer* is that the route must still draw
  // without one. This is that arrangement, observed: no provider, an explanation on the
  // map, and — every other test in this directory — a route underneath it.
  const response = await app.page.request.get("/api/basemap");
  expect(response.status()).toBe(200);
  expect(await response.json()).toMatchObject({ provider: null });

  await app.open();
  await expect(app.page.getByText("No basemap: LONGRUN_TILE_PROVIDER=none")).toBeVisible();
});
