/**
 * Reading a plan, as functions that take one and return a value (scope 10.3).
 *
 * A file of its own from M12, and the reason is M12.8: `ui/` had no automated verification
 * of any kind until this milestone, and the only things a headless run can check are the
 * ones with no MapLibre instance and no DOM in them. Scattering these through the
 * components would have left every one of them testable only by opening a browser.
 *
 * **Nothing here computes anything the backend does not already know.** Scope 3.9's rule
 * is that the UI adds no capability, and the test of that is whether a function here could
 * ever disagree with `core/`. `segmentRange` is the arithmetic `Segment` already carries
 * split into two names; `pointAtDistance` follows `core.plan.edits.distance_along`'s rule
 * deliberately rather than inventing a better one; `polygonFromVertices` shapes a ring and
 * **does not round it**, because rounding at `AREA_PRECISION` has exactly one owner and it
 * is `core.routing.avoid` on the server.
 *
 * **Absence is absence.** `ele_m: null` is a point nobody measured, and the two functions
 * that touch elevation are here so that the timeline draws a break rather than a line
 * through the middle of one. An edited route has a real gap - `splice` keeps the heights of
 * the ground it did not touch and leaves the new stretch's unknown - so this is not a
 * theoretical case in M12, it is what every `choose` produces.
 */
import { FLAG_KIND, TIER, type Flag, type Plan, type RoutePoint, type Segment } from "../api";

/**
 * Flags by segment id, worst first — what the map colours a segment by.
 *
 * **The comparator is arithmetic on `kind`, not a string test**, and that is M15's
 * correction rather than a style. `FlagKind` is an `IntEnum` on the wire, so the old
 * `a.kind === "hard" ? -1 : 1` was false for every flag that has ever arrived: whenever two
 * flags differed in kind it returned `1` for both orderings, which is not a comparator at
 * all. A soft flag could therefore end up at `[0]` and colour the segment, and a segment
 * whose real worst flag is a hard safety one would be painted for the wrong reason — on a
 * map somebody is reading for safety.
 */
export function flagsBySegment(plan: Plan): Map<string, Flag[]> {
  const out = new Map<string, Flag[]>();
  for (const result of plan.results ?? []) {
    for (const flag of result.flags ?? []) {
      const list = out.get(flag.segment_id) ?? [];
      list.push(flag);
      out.set(flag.segment_id, list);
    }
  }
  // Hard before soft — `FLAG_KIND.HARD` is the larger value, so descending on kind — and
  // severity descending within a kind.
  for (const list of out.values()) {
    list.sort((a, b) => (a.kind === b.kind ? b.severity - a.severity : b.kind - a.kind));
  }
  return out;
}

/**
 * The colour a tier gets on the map.
 *
 * Tier, not severity: scope 8.4 makes the tiers lexicographic — a safety flag outranks any
 * amount of discomfort — and a gradient over severity would put a 0.9 comfort flag and a
 * 0.9 safety flag in the same colour, which is exactly the comparison arbitration refuses
 * to make.
 *
 * **It takes a number, because `Tier` is an `IntEnum`.** Until M15 this switched on
 * `"safety"` and `"physiological"`, and every real flag carries `0` or `1`, so every branch
 * fell through to the default and the whole map was painted comfort blue. The tier colouring
 * `ui/README.md` leads with — "each segment coloured by its worst flag's tier" — had never
 * once happened on a screen.
 */
export function tierColour(tier: number): string {
  switch (tier) {
    case TIER.SAFETY:
      return "#d1495b";
    case TIER.PHYSIOLOGICAL:
      return "#e8a33d";
    default:
      return "#4a8fc2";
  }
}

/**
 * A tier as a word, for the hover popup.
 *
 * The popup exists so that somebody reading the map finds out *why* a segment is coloured,
 * and `${worst.tier}` on an `IntEnum` puts a `2` there — worse than the reason code the
 * popup was written to improve on. An unfamiliar tier is rendered as `tier N` rather than
 * guessed at, for the same reason `tierColour` declines to invent a colour for one.
 */
export function tierName(tier: number): string {
  switch (tier) {
    case TIER.SAFETY:
      return "safety";
    case TIER.PHYSIOLOGICAL:
      return "physiological";
    case TIER.COMFORT:
      return "comfort";
    default:
      return `tier ${tier}`;
  }
}

/** A flag kind as a word, for the same reason and with the same caution. */
export function kindName(kind: number): string {
  if (kind === FLAG_KIND.HARD) return "hard";
  if (kind === FLAG_KIND.SOFT) return "soft";
  return `kind ${kind}`;
}

/**
 * A segment as the pair of distances every write endpoint takes.
 *
 * **Metres, never the segment id.** `segment_id` is `f"s{index:05d}"` and positional, so
 * an id read off the map before an edit names different ground after one — that is the bug
 * M11.3 exists for, and sending one to a write endpoint would reintroduce it from the
 * client side. A distance names the same ground on both sides of a renumbering.
 */
export function segmentRange(segment: Segment): { startM: number; endM: number } {
  return { startM: segment.cum_start_m, endM: segment.cum_start_m + segment.length_m };
}

/** The segment covering `metres`, or null past either end. */
export function segmentAt(segments: Segment[], metres: number): Segment | null {
  for (const segment of segments) {
    const { startM, endM } = segmentRange(segment);
    if (metres >= startM && metres <= endM) return segment;
  }
  return null;
}

/**
 * Where on the line a distance falls, to the nearest sampled point.
 *
 * The rule `core.plan.edits.distance_along` uses, in the other direction and on purpose:
 * route points are far finer than "which kilometre mark is this" needs, and interpolating
 * between two of them would be the UI inventing geometry the backend never drew. `null`
 * when there is no line — which is not the origin, since the origin is a real place.
 */
export function pointAtDistance(points: RoutePoint[], metres: number): RoutePoint | null {
  let best: RoutePoint | null = null;
  let gap = Infinity;
  for (const point of points) {
    const distance = Math.abs(point.cum_dist_m - metres);
    if (distance < gap) {
      gap = distance;
      best = point;
    }
  }
  return best;
}

/**
 * The line broken into runs of consecutive points that carry an elevation.
 *
 * One run for a route that was sampled end to end, several for one that was edited. The
 * alternative — filtering the nulls out and drawing a single polyline through what is left
 * — draws a straight line across the unmeasured stretch, which reads as flat ground rather
 * than as no reading. That is the same mistake as rendering `detour_ratio: null` as `1.0`,
 * made with a pen instead of a number.
 */
export function elevationRuns(points: RoutePoint[]): RoutePoint[][] {
  const runs: RoutePoint[][] = [];
  let run: RoutePoint[] = [];
  for (const point of points) {
    if (point.ele_m === null || point.ele_m === undefined) {
      if (run.length > 1) runs.push(run);
      run = [];
    } else {
      run.push(point);
    }
  }
  if (run.length > 1) runs.push(run);
  return runs;
}

/**
 * How much of the line has no elevation reading, in metres.
 *
 * Reported rather than inferred from the gap in the drawing: `samples_missing` counts this
 * on the backend and a reader should not have to measure a chart to find out how much of it
 * is missing. Zero means every point was sampled, which is a different answer from a route
 * with no profile at all — `elevationRuns` returning nothing is that one.
 */
export function unmeasuredMetres(points: RoutePoint[]): number {
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    if (previous.ele_m === null || current.ele_m === null) {
      total += current.cum_dist_m - previous.cum_dist_m;
    }
  }
  return total;
}

/** A vertex as MapLibre hands one over. */
export interface Vertex {
  lng: number;
  lat: number;
}

/**
 * Vertices clicked on the map as the GeoJSON polygon the avoid endpoint takes.
 *
 * **Closed here, and rounded nowhere here.** Closing the ring is a GeoJSON spelling rule
 * and belongs to whoever writes the GeoJSON; rounding at `AREA_PRECISION` is a routing-cache
 * rule with exactly one owner, `core.routing.avoid`, and a client that did it too would be a
 * second implementation of the thing M5.13 was spent getting right once. Sixteen digits of
 * browser float go up the wire and the server decides what they are worth.
 *
 * `null` under three vertices: two points enclose no area, and the server would refuse it
 * with "the polygon encloses no area" after a round trip that told the user nothing.
 */
export function polygonFromVertices(vertices: Vertex[]): Record<string, unknown> | null {
  if (vertices.length < 3) return null;
  const ring = vertices.map((vertex) => [vertex.lng, vertex.lat]);
  const [firstLng, firstLat] = ring[0];
  const [lastLng, lastLat] = ring[ring.length - 1];
  if (firstLng !== lastLng || firstLat !== lastLat) ring.push([firstLng, firstLat]);
  return { type: "Polygon", coordinates: [ring] };
}

/** Vertices clicked on the map as the `'lat,lon'` points the choose endpoint takes. */
export function lineFromVertices(vertices: Vertex[]): string[] | null {
  if (vertices.length < 2) return null;
  return vertices.map((vertex) => `${vertex.lat},${vertex.lng}`);
}
