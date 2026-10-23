import { describe, expect, it } from "vitest";
import type { LabelQueue, ReviewTask } from "../api";
import {
  answerBody,
  boundsOf,
  describeAnswer,
  lineFeatures,
  progress,
  shareRows,
} from "./review";

const TASK: ReviewTask = {
  id: "t1",
  region: "bayarea",
  probe: false,
  a: {
    points: [
      [37.83, -122.53],
      [37.84, -122.5],
    ],
    length_m: 3500,
    diverges: [[[37.83, -122.53]], [[37.835, -122.52], [37.836, -122.51]]],
  },
  b: {
    points: [
      [37.83, -122.53],
      [37.82, -122.49],
    ],
    length_m: 4000,
    diverges: [],
  },
  answer: null,
  shares: null,
};

describe("the Review page, not the rule", () => {
  it("wants a strength with a choice and none with a non-choice", () => {
    expect(answerBody("a", null, "").problems).toHaveLength(1);
    expect(answerBody("b", "slight", " quieter ").body).toEqual({
      prefer: "b",
      strength: "slight",
      reason: "quieter",
    });
    // A strength left over from an earlier click is not sent with "no preference".
    expect(answerBody("none", "clear", "").body).toEqual({ prefer: "none" });
  });

  it("draws lines as GeoJSON, which is lon-first, from the ledger's lat-first points", () => {
    const features = lineFeatures(TASK.a, "a");
    expect(features.line.features[0].geometry.coordinates[0]).toEqual([-122.53, 37.83]);
    // A one-point stretch is not a line and is not drawn.
    expect(features.diverges.features).toHaveLength(1);
  });

  it("frames both lines", () => {
    expect(boundsOf(TASK)).toEqual([
      [-122.53, 37.82],
      [-122.49, 37.84],
    ]);
  });

  it("says an answer in words", () => {
    const at = { labelled_at: "", via: "ui", reason: null };
    expect(describeAnswer({ prefer: "a", strength: "slight", ...at })).toBe("Slightly A");
    expect(describeAnswer({ prefer: "none", strength: null, ...at })).toBe("No preference");
  });

  it("lays the revealed numbers out side by side", () => {
    const rows = shareRows({
      a: { lts3_plus: 0.25, unpaved: 0, path: 0.1 },
      b: { lts3_plus: 0, unpaved: 0.86, path: 0.92 },
    });
    expect(rows[0]).toEqual({ label: "On LTS 3 or worse", a: "25%", b: "0%" });
    expect(rows[2].b).toBe("92%");
  });

  it("counts what tune will count, and says when asked-twice answers disagree", () => {
    const queue: LabelQueue = {
      pending: 3,
      answered: 5,
      next: "x",
      labeler: "r",
      counts: { a: 2, b: 1, none: 1, cant: 1 },
      probes: { agree: 1, total: 2 },
    };
    expect(progress(queue)).toBe(
      "5 answered, 3 to go · 3 chose a line · 1 no preference · asked-twice 1/2 agree",
    );
  });
});
