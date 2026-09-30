import { describe, expect, it } from "vitest";
import {
  bodyFrom,
  boundable,
  emptyForm,
  nextRow,
  shown,
  wordCount,
  type MetricRow,
  type VerdictForm,
} from "./verdict";

const METRICS: MetricRow[] = [
  { name: "fraction_lts3_plus", meaning: "share at LTS 3+", value: 0.2 },
  { name: "detour_ratio", meaning: "detour", value: null },
  { name: "lts4_count", meaning: "LTS 4 segments", value: 4 },
];

function form(overrides: Partial<VerdictForm>): VerdictForm {
  return { ...emptyForm(), wouldRun: "no", reason: "a reason", ...overrides };
}

describe("the form, not the rule", () => {
  it("offers only a metric this plan has a value for", () => {
    expect(boundable(METRICS).map((m) => m.name)).toEqual(["fraction_lts3_plus", "lts4_count"]);
  });

  it("starts a new row with an empty limit, never the measured value", () => {
    // A limit filled in from the plan's own value makes "a `no` breaches its bound" pass by
    // construction (ADR 0022) — so the box is empty and the value is shown beside it.
    expect(nextRow(METRICS, [])).toEqual({ metric: "fraction_lts3_plus", side: "max", limit: "" });
  });

  it("does not offer a bound that is already stated, and runs out", () => {
    const rows = [
      { metric: "fraction_lts3_plus", side: "max" as const, limit: "" },
      { metric: "fraction_lts3_plus", side: "min" as const, limit: "" },
      { metric: "lts4_count", side: "max" as const, limit: "" },
    ];
    expect(nextRow(METRICS, rows)).toEqual({ metric: "lts4_count", side: "min", limit: "" });
    expect(nextRow(METRICS, [...rows, { metric: "lts4_count", side: "min", limit: "" }])).toBeNull();
  });

  it("builds the body the server takes", () => {
    const { body, problems } = bodyFrom(
      form({ rows: [{ metric: "lts4_count", side: "max", limit: "2" }], note: " n ", golden: "g" }),
    );
    expect(problems).toEqual([]);
    expect(body).toEqual({
      would_run: "no",
      reason: "a reason",
      bounds: { lts4_count_max: 2 },
      note: "n",
      golden: "g",
    });
  });

  it("keeps an empty box apart from a zero", () => {
    expect(bodyFrom(form({ rows: [{ metric: "lts4_count", side: "max", limit: "0" }] })).body?.bounds)
      .toEqual({ lts4_count_max: 0 });
    expect(bodyFrom(form({ rows: [{ metric: "lts4_count", side: "max", limit: " " }] })).problems)
      .toEqual(["Give lts4_count_max a limit, or remove the row."]);
  });

  it("refuses the same bound twice and a limit that is not a number", () => {
    const { body, problems } = bodyFrom(
      form({
        rows: [
          { metric: "lts4_count", side: "max", limit: "2" },
          { metric: "lts4_count", side: "max", limit: "3" },
          { metric: "fraction_lts3_plus", side: "max", limit: "lots" },
        ],
      }),
    );
    expect(body).toBeNull();
    expect(problems).toEqual([
      "lts4_count_max is stated twice.",
      'fraction_lts3_plus_max: "lots" is not a number.',
    ]);
  });

  it("sends a gap only for a no, and only with words in it", () => {
    expect(bodyFrom(form({ useGap: true, gap: "floods at high tide" })).body?.gap).toBe(
      "floods at high tide",
    );
    expect(bodyFrom(form({ wouldRun: "yes", useGap: true, gap: "x" })).body?.gap).toBeUndefined();
    expect(bodyFrom(form({ useGap: true, gap: "" })).problems).toHaveLength(1);
  });

  it("asks for an answer before anything else", () => {
    expect(bodyFrom({ ...emptyForm(), reason: "r" }).problems).toEqual([
      "Choose yes, no or unsure.",
    ]);
  });

  it("leaves the reason's length to the server", () => {
    // Twelve words is `check_verdict`'s rule; the panel shows a count and does not enforce it.
    expect(bodyFrom(form({ reason: "short" })).problems).toEqual([]);
    expect(wordCount("  two  words ")).toBe(2);
  });

  it("prints an absent number as absent", () => {
    expect(shown(null)).toBe("—");
    expect(shown(0)).toBe("0");
    expect(shown(0.02345)).toBe("0.023");
  });
});
