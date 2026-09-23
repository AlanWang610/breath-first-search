/**
 * Driving the real page: how a test turns "segment 9" into a pixel, and how it waits out
 * an asynchronous write.
 *
 * **The hard part is that nothing exposes the map.** `MapView` keeps its `maplibregl.Map`
 * in a React ref. There is no `window.map`, and there must not be: a handle added so a test
 * could call `project()` would be production code that exists for the suite, and the next
 * person would reasonably use it. MapLibre is bundled rather than global, so it cannot be
 * patched from an init script either.
 *
 * So the projection is read back out of the application instead, through two things it
 * already draws.
 *
 * **The timeline cursor is a projected route point.** M12.7 put a marker on the map at
 * `pointAtDistance(plan.route.points, cursorM)` — the nearest sampled point, never an
 * interpolation — and a MapLibre `Marker` is a DOM element positioned by a CSS transform.
 * Scrub the strip to a distance and the marker's box *is* `map.project()` of a point on the
 * line, answered by the application's own camera. That is why `pixelOnRoute` scrubs: it is
 * not testing the timeline as a side effect, it is asking the map where it is pointing.
 *
 * **The hover popup confirms the answer.** The marker's box centre is not quite the
 * projected point: MapLibre's default pin has its tip at the location and its element
 * offset by `[0, -14]` so the tip lands there. Rather than trust that constant, the
 * candidate pixel is *probed* — move the pointer, read the popup, and accept the first
 * position where the popup names the segment that was asked for. A pixel that cannot be
 * confirmed fails the test with the reason, instead of producing a click that lands on
 * nothing and a selection panel that stays empty for an unexplained reason.
 *
 * It also means the suite never asserts anything about where the map *should* have put a
 * point. It asks where the map put it and works from there, so a camera change makes the
 * test do different arithmetic rather than fail.
 *
 * **Waiting out a write.** Every gesture is a 202 and a 1.5 s poll (`POLL_MS` in
 * `App.tsx`), and when the poll sees a terminal status it calls `openPlan`, which re-reads
 * the plan and re-renders the map. So "the write is done" is not "the POST returned"; it is
 * the `GET /api/plans/{id}` that the poll's `openPlan` issues afterwards. `write()` arms a
 * listener for exactly that response *before* the gesture, which is the only ordering that
 * cannot miss it, and settles briefly afterwards so the draw effect has run. Anything
 * asserting that the camera survived a write has to be on the far side of both.
 */
import { expect, test as base, type Locator, type Page } from "@playwright/test";
import { seedPlan } from "./seed";

/** A pixel in page coordinates. */
export interface Pixel {
  x: number;
  y: number;
}

/**
 * MapLibre's default marker offset: the pin's tip is at the location, and the element sits
 * 14 px above its own centre so that it lands there. Used only as the *starting guess* for
 * the probe below, which is why an upstream change to it would cost a probe and not a test.
 */
const MARKER_TIP_OFFSET_PX = 14;

/** How far from the guess the probe will look, and how finely. */
const PROBE_RADIUS_PX = 24;
const PROBE_STEP_PX = 3;

/**
 * A plan as the server dumps it.
 *
 * Declared here rather than reused from `src/api.ts` on purpose. Those types are the
 * client's hand-written view, and M15 found two of them wrong against the wire — a test
 * that imported them would inherit whatever mistake they hold and confirm it. This is what
 * the suite reads out of the response, and `contract.spec.ts` is where the two are made to
 * agree.
 */
export interface StoredPlan {
  id: string;
  request: {
    locked: { start_m: number; end_m: number; reason: string | null; source: string }[];
    via: { lat: number; lon: number }[];
    avoid_polygons: Record<string, unknown>[];
  };
  route: { points: { lat: number; lon: number; cum_dist_m: number; ele_m: number | null }[] };
  segments: { id: string; index: number; cum_start_m: number; length_m: number }[];
  results: {
    name: string;
    flags: {
      scorer: string;
      segment_id: string;
      kind: unknown;
      tier: unknown;
      severity: number;
      reason_code: string;
      detail: string | null;
    }[];
  }[];
}

export class App {
  constructor(
    readonly page: Page,
    readonly planId: string,
  ) {}

  /** The plan as the server holds it right now — the only witness to what a write did. */
  async stored(): Promise<StoredPlan> {
    const response = await this.page.request.get(`/api/plans/${this.planId}`);
    expect(response.status(), "the seeded plan should be readable").toBe(200);
    return (await response.json()) as StoredPlan;
  }

  /** Load the page and open this plan, then wait until the map has drawn its route. */
  async open(): Promise<void> {
    await this.page.goto("/");
    await this.switchTo();
    // The route reaching the *rendered* frame is a separate event from React rendering, and
    // `queryRenderedFeatures` reads the frame. Probing for a segment is how a test finds
    // out the difference has passed.
    await this.pixelOnRoute(await this.midOf(9));
  }

  /**
   * Open this plan in a page that is already loaded, from the stored-plans list.
   *
   * Separate from `open()` because a reload builds a new map, which fits unconditionally.
   * Anything asking what the camera does when the *plan* changes has to change the plan
   * inside one map, which is what a user does.
   */
  async switchTo(): Promise<void> {
    await this.page.getByRole("button", { name: this.planId, exact: true }).click();
    // The edit panel exists only when a plan is open, so it is the render's own signal.
    await expect(this.page.getByRole("heading", { name: "Edit this plan" })).toBeVisible();
    await expect(this.page.locator("canvas.maplibregl-canvas")).toBeVisible();
    await expect(this.page.locator("section.timeline")).toBeVisible();
    // `fitBounds` runs with `duration: 0`, so this is a render settle and not an animation.
    await this.page.waitForTimeout(400);
  }

  /** The distance at the middle of segment `index`, which is the least ambiguous pixel. */
  async midOf(index: number): Promise<number> {
    const segment = (await this.storedSegments())[index];
    if (!segment) throw new Error(`the seeded plan has no segment ${index}`);
    return segment.cum_start_m + segment.length_m / 2;
  }

  private cachedSegments: StoredPlan["segments"] | null = null;
  private async storedSegments(): Promise<StoredPlan["segments"]> {
    // Read once. A gesture rewrites `plan.json`, and `segment_id` is positional — but the
    // *geometry* of a request edit does not move, so the distances stay valid across a
    // lock, an unlock, a via and an avoid. A `choose` does move it, and that test re-reads.
    this.cachedSegments ??= (await this.stored()).segments;
    return this.cachedSegments;
  }

  /** The id of segment `index`, as the popup and the selection panel spell it. */
  async segmentId(index: number): Promise<string> {
    return (await this.storedSegments())[index].id;
  }

  private timeline(): Locator {
    return this.page.locator("section.timeline svg");
  }

  /**
   * Scrub the timeline to `metres` and hand back where the map put that point.
   *
   * Two gestures at once, deliberately: the scrub is scope 10.3's "scrubbing highlights the
   * map position", and its answer is the projection every other gesture needs.
   */
  async cursorPixel(metres: number): Promise<Pixel> {
    const strip = this.timeline();
    const box = await strip.boundingBox();
    if (!box) throw new Error("the timeline strip has no box; is a plan open?");
    const total = await this.totalMetres();
    const fraction = Math.min(1, Math.max(0, metres / total));
    await this.page.mouse.move(box.x + fraction * box.width, box.y + box.height / 2);

    const marker = this.page.locator(".maplibregl-marker").first();
    await expect(marker, "scrubbing the timeline should put a marker on the map").toBeVisible();
    const markerBox = await marker.boundingBox();
    if (!markerBox) throw new Error("the cursor marker has no box");
    return {
      x: markerBox.x + markerBox.width / 2,
      y: markerBox.y + markerBox.height / 2 + MARKER_TIP_OFFSET_PX,
    };
  }

  private cachedTotal: number | null = null;
  private async totalMetres(): Promise<number> {
    if (this.cachedTotal === null) {
      const points = (await this.stored()).route.points;
      this.cachedTotal = points[points.length - 1].cum_dist_m;
    }
    return this.cachedTotal;
  }

  /**
   * The prose the map shows for whatever is under `pixel`, or `null` for bare ground.
   *
   * This is the hover gesture itself: `MapView`'s `mousemove` handler queries the rendered
   * frame and puts `${segment.id}: ${scorer} ${kind} (${tier}) — ${detail}` in a popup.
   */
  async hoverReason(pixel: Pixel): Promise<string | null> {
    await this.page.mouse.move(pixel.x, pixel.y);
    // A short settle rather than a locator wait: a miss is the common case while probing,
    // and a locator timeout per miss would make the search cost seconds instead of
    // milliseconds.
    await this.page.waitForTimeout(25);
    return this.page.evaluate(
      () => document.querySelector(".maplibregl-popup-content")?.textContent ?? null,
    );
  }

  /**
   * A pixel that is genuinely on the drawn line at `metres`, confirmed by the popup.
   *
   * The guess comes from the cursor marker and the search spirals outward from it. The
   * first position whose popup names the expected segment wins; twenty-four pixels is
   * further than the marker offset could be wrong and still narrower than a segment, so a
   * hit is the segment that was asked for and not its neighbour.
   *
   * **Only answers while the map is in its selecting state.** In a draw mode `MapView`'s
   * `mousemove` handler removes the popup and sets a crosshair without querying anything,
   * which is correct — the map is not asking what is under the pointer then. A test that
   * needs a pixel for a via or a polygon corner takes it before entering the mode.
   */
  async pixelOnRoute(metres: number | Promise<number>): Promise<Pixel> {
    const target = await metres;
    const expectedId = await this.segmentIdAt(target);
    const guess = await this.cursorPixel(target);
    for (const candidate of spiral(guess)) {
      const reason = await this.hoverReason(candidate);
      if (reason?.startsWith(`${expectedId}:`)) return candidate;
    }
    throw new Error(
      `no pixel within ${PROBE_RADIUS_PX}px of the cursor marker showed ${expectedId}; ` +
        "either the route is not on the rendered frame or the camera moved mid-probe",
    );
  }

  private async segmentIdAt(metres: number): Promise<string> {
    const segments = await this.storedSegments();
    const found = segments.find(
      (segment) => metres >= segment.cum_start_m && metres < segment.cum_start_m + segment.length_m,
    );
    if (!found) throw new Error(`no segment covers ${metres} m`);
    return found.id;
  }

  /** Click a segment, and wait for the panel to say which stretch is now selected. */
  async selectSegment(index: number): Promise<string> {
    const id = await this.segmentId(index);
    const pixel = await this.pixelOnRoute(this.midOf(index));
    await this.page.mouse.click(pixel.x, pixel.y);
    await expect(this.page.locator("section.panel p.small").first()).toContainText(id);
    return id;
  }

  /**
   * Run a gesture and wait until the plan it changed has been re-read and re-drawn.
   *
   * The listener is armed before the action, because a 202 plus a 1.5 s poll means the
   * reload can land at any point after it and a listener attached afterwards would race it.
   */
  async write(action: () => Promise<void>): Promise<void> {
    const reloaded = this.page.waitForResponse(
      (response) =>
        response.url().includes(`/api/plans/${this.planId}`) &&
        response.request().method() === "GET" &&
        response.status() === 200,
      { timeout: 60_000 },
    );
    await action();
    await reloaded;
    await expect(this.page.getByRole("heading", { name: /^Job/ })).toContainText("complete");
    // The draw effect runs on the new plan object after that response resolves. Half a
    // second is far more than a React render and a `setData`, and it is the window in which
    // a `fitBounds` that should not have happened would happen.
    await this.page.waitForTimeout(500);
  }

  /** Drag the map, with the pause that stops MapLibre's inertia carrying it further. */
  async panBy(dx: number, dy: number): Promise<void> {
    const canvas = this.page.locator("canvas.maplibregl-canvas");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("the map canvas has no box");
    const from = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    await this.page.mouse.move(from.x, from.y);
    await this.page.mouse.down();
    for (let step = 1; step <= 8; step += 1) {
      await this.page.mouse.move(from.x + (dx * step) / 8, from.y + (dy * step) / 8);
    }
    // MapLibre's inertia only looks at movement in the last ~160 ms, so a pause here is the
    // difference between panning by `dx` and panning by an amount nobody chose.
    await this.page.waitForTimeout(300);
    await this.page.mouse.up();
    await this.page.waitForTimeout(300);
  }
}

/** Candidate pixels in rings around `centre`, nearest first. */
function* spiral(centre: Pixel): Generator<Pixel> {
  yield centre;
  for (let radius = PROBE_STEP_PX; radius <= PROBE_RADIUS_PX; radius += PROBE_STEP_PX) {
    for (const [dx, dy] of [
      [0, radius],
      [0, -radius],
      [radius, 0],
      [-radius, 0],
      [radius, radius],
      [-radius, radius],
      [radius, -radius],
      [-radius, -radius],
    ]) {
      yield { x: centre.x + dx, y: centre.y + dy };
    }
  }
}

/** A plan id from a test title: lower case, hyphens, and nothing `_plan_id_dir` refuses. */
function planIdFor(title: string): string {
  const slug = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return `gesture-${slug || "unnamed"}`;
}

/**
 * Every test gets its own plan, copied fresh, because every gesture mutates one.
 *
 * Named after the test so a failure leaves a directory somebody can open, rather than a
 * uuid they would have to correlate.
 */
export const test = base.extend<{ app: App }>({
  app: async ({ page }, use, testInfo) => {
    // The file stem is in the id too: two specs may reasonably name a test the same thing,
    // and two tests sharing a plan directory is the cross-test mutation this fixture exists
    // to prevent.
    const stem = testInfo.file.split(/[\\/]/).pop()?.replace(/\.spec\.ts$/, "") ?? "gesture";
    await use(new App(page, seedPlan(planIdFor(`${stem}-${testInfo.title}`))));
  },
});

export { expect } from "@playwright/test";
