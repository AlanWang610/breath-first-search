/**
 * The Review page's arithmetic, as pure functions (ADR 0049).
 *
 * As with `lib/verdict`, the rules about an answer are the server's — a choice needs a
 * strength, a first answer stands — and this checks only the shape of what was clicked.
 * What does live here is what the page draws: two lines as GeoJSON, the stretches where
 * they part, and the box that holds both.
 */
import type { LabelQueue, LineShares, ReviewLine, ReviewTask } from "../api";

export type Prefer = "a" | "b" | "none" | "cant";
export type Strength = "slight" | "clear";

export interface AnswerBody {
  prefer: Prefer;
  strength?: Strength;
  reason?: string;
}

/** The two lines' colours. Not the tier colours: this page has no tiers, and a line that
 *  looked like a safety flag would be a thumb on the scale. */
export const LINE_COLOURS = { a: "#4fa3e0", b: "#e0864f" } as const;

export function answerBody(
  prefer: Prefer,
  strength: Strength | null,
  reason: string,
): { body: AnswerBody | null; problems: string[] } {
  const choosing = prefer === "a" || prefer === "b";
  if (choosing && strength === null) {
    return { body: null, problems: ["Say how strongly: slightly or clearly."] };
  }
  const body: AnswerBody = { prefer };
  if (choosing && strength !== null) body.strength = strength;
  if (reason.trim()) body.reason = reason.trim();
  return { body, problems: [] };
}

function lineString(points: [number, number][]) {
  return {
    type: "LineString" as const,
    coordinates: points.map(([lat, lon]) => [lon, lat]),
  };
}

/** One line and the stretches where it leaves the other, as GeoJSON. */
export function lineFeatures(line: ReviewLine, which: "a" | "b") {
  return {
    line: {
      type: "FeatureCollection" as const,
      features: [{ type: "Feature" as const, properties: { which }, geometry: lineString(line.points) }],
    },
    diverges: {
      type: "FeatureCollection" as const,
      features: line.diverges
        .filter((stretch) => stretch.length >= 2)
        .map((stretch) => ({
          type: "Feature" as const,
          properties: { which },
          geometry: lineString(stretch),
        })),
    },
  };
}

/** [[west, south], [east, north]] around both lines, or `null` for a task with none. */
export function boundsOf(task: ReviewTask): [[number, number], [number, number]] | null {
  const points = [...task.a.points, ...task.b.points];
  if (points.length === 0) return null;
  const lats = points.map(([lat]) => lat);
  const lons = points.map(([, lon]) => lon);
  return [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
}

export function km(metres: number): string {
  return `${(metres / 1000).toFixed(2)} km`;
}

export function percent(share: number): string {
  return `${Math.round(share * 100)}%`;
}

/** The revealed numbers, one row per measure, for the table shown after an answer. */
export function shareRows(shares: { a: LineShares; b: LineShares }) {
  return (
    [
      ["On LTS 3 or worse", "lts3_plus"],
      ["Unpaved", "unpaved"],
      ["On a path or footway", "path"],
    ] as const
  ).map(([label, key]) => ({ label, a: percent(shares.a[key]), b: percent(shares.b[key]) }));
}

/** What the answer said, in words. */
export function describeAnswer(answer: NonNullable<ReviewTask["answer"]>): string {
  if (answer.prefer === "none") return "No preference";
  if (answer.prefer === "cant") return "Could not judge";
  return `${answer.strength === "slight" ? "Slightly" : "Clearly"} ${answer.prefer.toUpperCase()}`;
}

/** The session line: how much is done, and how much of it tune will count. */
export function progress(queue: LabelQueue): string {
  const chose = queue.counts.a + queue.counts.b;
  const parts = [
    `${queue.answered} answered, ${queue.pending} to go`,
    `${chose} chose a line`,
    `${queue.counts.none} no preference`,
  ];
  if (queue.probes.total > 0) {
    parts.push(`asked-twice ${queue.probes.agree}/${queue.probes.total} agree`);
  }
  return parts.join(" · ");
}
