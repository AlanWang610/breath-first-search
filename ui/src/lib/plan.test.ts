/**
 * The first automated verification `ui/` has ever had (M12.8).
 *
 * `package.json` had `typecheck` and no test runner, and `.github/workflows/ci.yml` never
 * entered this directory, so the only thing standing between a regression and `main` was
 * somebody opening a browser. That is not a gap worth mentioning in a milestone that adds
 * five write paths to the same components — it is the only thing that will catch a
 * regression in this milestone's own work.
 *
 * **What is checked here is what a headless run can honestly check**: functions of a plan
 * that return a value. A MapLibre drag, a click that places a polygon vertex, a popup on
 * hover — none of those are covered, by anything, and the PR says so plainly rather than
 * counting a mounted component as evidence that a gesture works.
 *
 * Two of the cases below exist because the bug they describe shipped. `tierColour` and
 * `flagsBySegment` are the two functions the map has always depended on and nothing ever
 * checked; `TradeOff` is the one `tsc` agreed with because the types here are hand-written
 * and it was checking a mistake against itself.
 */
import { describe, expect, it } from "vitest";
import { FLAG_KIND, TIER, type Plan, type RoutePoint, type Segment } from "../api";
import {
  elevationRuns,
  flagsBySegment,
  kindName,
  lineFromVertices,
  pointAtDistance,
  polygonFromVertices,
  segmentAt,
  segmentRange,
  tierColour,
  tierName,
  unmeasuredMetres,
} from "./plan";

function point(index: number, ele: number | null): RoutePoint {
  return { lat: 37.77 + index * 0.001, lon: -122.41, ele_m: ele, cum_dist_m: index * 100 };
}

function segment(index: number, startM: number, lengthM: number): Segment {
  return {
    id: `s${String(index).padStart(5, "0")}`,
    index,
    start_idx: index,
    end_idx: index + 1,
    cum_start_m: startM,
    length_m: lengthM,
    way_id: null,
  };
}

function plan(results: Plan["results"]): Plan {
  return {
    id: "p",
    status: "complete",
    route: { id: "r", points: [point(0, 10), point(1, 20)] },
    segments: [segment(0, 0, 100)],
    results,
    etas: [],
    residual_flags: [],
    trade_offs: [],
    warnings: [],
    pacing_caveats: [],
    coverage: { entries: [] },
    metrics: {},
    elevation: null,
    profile: {},
  };
}

describe("tierColour", () => {
  // **The numbers are the fixture, and that is M15's correction.** `Tier` is an `IntEnum`,
  // so a plan off the wire carries `0`, `1`, `2`. These cases used to pass `"safety"` and
  // `"comfort"` - spellings no server has ever sent - so they agreed with `api.ts`, which
  // was wrong in the same direction, and every real flag fell through to the default. The
  // map was painted one colour and a green suite said otherwise.
  it("gives each tier its own colour", () => {
    expect(tierColour(TIER.SAFETY)).toBe("#d1495b");
    expect(tierColour(TIER.PHYSIOLOGICAL)).toBe("#e8a33d");
    expect(tierColour(TIER.COMFORT)).toBe("#4a8fc2");
  });

  it("does not invent a colour for a tier it has not met", () => {
    // Scope 8.4's tiers are lexicographic and comfort is the bottom one, so an unknown
    // tier rendering as comfort understates rather than overstates. A thrown error or a
    // transparent line would both be worse on a map somebody is reading for safety.
    expect(tierColour(7)).toBe(tierColour(TIER.COMFORT));
  });
});

describe("tierName and kindName", () => {
  it("say the word the popup needs, because a runner cannot read an IntEnum", () => {
    // The hover exists so somebody finds out *why* a segment is coloured. Interpolating
    // the enum put `hazards 0 (2)` in the popup - less use than the reason code the prose
    // was written to improve on.
    expect(tierName(TIER.SAFETY)).toBe("safety");
    expect(tierName(TIER.PHYSIOLOGICAL)).toBe("physiological");
    expect(tierName(TIER.COMFORT)).toBe("comfort");
    expect(kindName(FLAG_KIND.HARD)).toBe("hard");
    expect(kindName(FLAG_KIND.SOFT)).toBe("soft");
  });

  it("names an unfamiliar value rather than guessing at it", () => {
    // Same rule as `tierColour`: a tier this UI has not met is reported as the number it
    // is, never folded into one it recognises.
    expect(tierName(9)).toBe("tier 9");
    expect(kindName(9)).toBe("kind 9");
  });
});

describe("flagsBySegment", () => {
  it("puts hard flags before soft ones whatever their severity", () => {
    // The map colours a segment by `[0]`, and scope 8.4 makes the tiers lexicographic: a
    // 0.1 hard flag outranks a 0.9 soft one, and sorting on severity would paint the
    // segment for the wrong reason.
    const byId = flagsBySegment(
      plan([
        {
          name: "x",
          flags: [
            { scorer: "x", segment_id: "s00000", kind: FLAG_KIND.SOFT, tier: TIER.COMFORT, severity: 0.9, reason_code: "a" },
            { scorer: "y", segment_id: "s00000", kind: FLAG_KIND.HARD, tier: TIER.SAFETY, severity: 0.1, reason_code: "b" },
          ],
        },
      ]),
    );

    expect(byId.get("s00000")?.map((flag) => flag.reason_code)).toEqual(["b", "a"]);
  });

  it("orders same-kind flags by severity", () => {
    const byId = flagsBySegment(
      plan([
        {
          name: "x",
          flags: [
            { scorer: "x", segment_id: "s00000", kind: FLAG_KIND.SOFT, tier: TIER.COMFORT, severity: 0.2, reason_code: "low" },
            { scorer: "y", segment_id: "s00000", kind: FLAG_KIND.SOFT, tier: TIER.COMFORT, severity: 0.8, reason_code: "high" },
          ],
        },
      ]),
    );

    expect(byId.get("s00000")?.map((flag) => flag.reason_code)).toEqual(["high", "low"]);
  });

  it("is empty rather than absent for a plan nothing flagged", () => {
    expect(flagsBySegment(plan([{ name: "x", flags: [] }])).size).toBe(0);
  });
});

describe("segmentRange", () => {
  it("is the pair of distances a write endpoint takes", () => {
    // Metres, never the id. `segment_id` is `f"s{index:05d}"` and positional, so an id
    // read off the map before an edit names different ground after one (M11.3) — sending
    // one to a write endpoint would reintroduce that bug from the client side.
    expect(segmentRange(segment(3, 1200, 340))).toEqual({ startM: 1200, endM: 1540 });
  });

  it("keeps a zero start distinct from a missing one", () => {
    // Zero is the origin of the route, which is a real place. The map's click handler
    // reads `Number(properties.start_m ?? 0)`, so a segment that genuinely begins at zero
    // and one whose property never arrived would be indistinguishable downstream — this
    // pins the only half of that pair this file owns.
    expect(segmentRange(segment(0, 0, 100)).startM).toBe(0);
  });
});

describe("segmentAt", () => {
  const segments = [segment(0, 0, 100), segment(1, 100, 150), segment(2, 250, 50)];

  it("finds the segment a distance falls in", () => {
    expect(segmentAt(segments, 180)?.id).toBe("s00001");
  });

  it("answers null past the end rather than clamping to the last segment", () => {
    // Clamping would report the finish as the answer to "what is at 40 km" on a 30 km
    // route, which is a plausible-looking wrong answer — the kind this project refuses.
    expect(segmentAt(segments, 9000)).toBeNull();
  });
});

describe("pointAtDistance", () => {
  const points = [point(0, 10), point(1, 20), point(2, 30)];

  it("takes the nearest sampled point rather than interpolating", () => {
    // `core.plan.edits.distance_along`'s rule, on purpose: route points are far finer than
    // "which kilometre mark is this" needs, and interpolating would be the UI drawing
    // geometry the backend never did.
    expect(pointAtDistance(points, 140)?.cum_dist_m).toBe(100);
    expect(pointAtDistance(points, 160)?.cum_dist_m).toBe(200);
  });

  it("is null for a line with no points", () => {
    expect(pointAtDistance([], 100)).toBeNull();
  });
});

describe("elevationRuns and unmeasuredMetres", () => {
  it("breaks the profile where an edit left no reading", () => {
    // What every `choose` produces: `splice` keeps the heights of the ground it did not
    // touch and leaves the replacement's unknown. Filtering the nulls out and drawing one
    // polyline through the rest puts a straight line across the gap, and a straight line on
    // an elevation chart reads as flat ground — the same error as rendering a null
    // `detour_ratio` as 1.0, made with a pen instead of a number.
    const line = [point(0, 10), point(1, 12), point(2, null), point(3, null), point(4, 20), point(5, 22)];

    expect(elevationRuns(line).map((run) => run.length)).toEqual([2, 2]);
  });

  it("reports the unmeasured distance rather than leaving it to be eyeballed", () => {
    const line = [point(0, 10), point(1, 12), point(2, null), point(3, 20)];

    // 100 m into the gap and 100 m out of it.
    expect(unmeasuredMetres(line)).toBe(200);
  });

  it("is zero for a line sampled end to end, and no runs for one with no profile at all", () => {
    // Two different answers: "every point was measured" and "nothing was". A single
    // number could not tell them apart, which is why there are two functions.
    const measured = [point(0, 10), point(1, 12)];
    const unmeasured = [point(0, null), point(1, null)];

    expect(unmeasuredMetres(measured)).toBe(0);
    expect(elevationRuns(measured)).toHaveLength(1);
    expect(elevationRuns(unmeasured)).toHaveLength(0);
  });
});

describe("polygonFromVertices", () => {
  const square = [
    { lng: -122.41, lat: 37.77 },
    { lng: -122.4, lat: 37.77 },
    { lng: -122.4, lat: 37.78 },
  ];

  it("closes the ring, because GeoJSON says a polygon is closed", () => {
    const polygon = polygonFromVertices(square) as { coordinates: number[][][] };
    const ring = polygon.coordinates[0];

    expect(ring).toHaveLength(4);
    expect(ring[0]).toEqual(ring[ring.length - 1]);
  });

  it("does not close an already-closed ring twice", () => {
    const closed = [...square, { lng: -122.41, lat: 37.77 }];
    const polygon = polygonFromVertices(closed) as { coordinates: number[][][] };

    expect(polygon.coordinates[0]).toHaveLength(4);
  });

  it("does not round, because rounding has one owner and it is the server", () => {
    // M5.13: an avoid area travels inside `custom_model` and `CachedRouter` hashes that
    // into its key, so `AREA_PRECISION` is a routing-cache rule enforced in
    // `core.routing.avoid`. A client that rounded too would be a second implementation of
    // the thing that milestone was spent getting right once — and the two would drift.
    const precise = [
      { lng: -122.4194123456789, lat: 37.7749123456789 },
      { lng: -122.4184123456789, lat: 37.7749123456789 },
      { lng: -122.4184123456789, lat: 37.7759123456789 },
    ];
    const polygon = polygonFromVertices(precise) as { coordinates: number[][][] };

    expect(polygon.coordinates[0][0][0]).toBe(-122.4194123456789);
  });

  it("is null under three corners rather than sending something that encloses nothing", () => {
    // The server would refuse it with "the polygon encloses no area" after a round trip
    // that told the user nothing they could not have been told immediately.
    expect(polygonFromVertices(square.slice(0, 2))).toBeNull();
    expect(polygonFromVertices([])).toBeNull();
  });
});

describe("lineFromVertices", () => {
  it("sends 'lat,lon', which is the spelling every coordinate on this API uses", () => {
    // And geometry rather than a path: a POST that took a GPX filename would give the
    // `tools/` layer's file-path convention write semantics over HTTP (ADR 0036).
    expect(lineFromVertices([{ lng: -122.4, lat: 37.77 }, { lng: -122.39, lat: 37.78 }])).toEqual([
      "37.77,-122.4",
      "37.78,-122.39",
    ]);
  });

  it("is null for a single point, which is not a line", () => {
    expect(lineFromVertices([{ lng: -122.4, lat: 37.77 }])).toBeNull();
  });
});
