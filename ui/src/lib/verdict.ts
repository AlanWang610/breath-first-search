/**
 * The Judge panel's form, as pure functions (ADR 0048).
 *
 * **What is not here is the rule.** Whether a verdict stands — a `no` has to breach one of
 * its own bounds, a bound on an unmeasured number is refused, a reason is a sentence — is
 * `core.labels.bounds.check_verdict`, and the server answers with its sentences verbatim.
 * A copy of it here would be a second owner, and the copy that drifted would be the one a
 * person was looking at. What this file checks is only the shape of the *form*: a bound row
 * with no number in it, the same bound twice, no answer chosen. Those are things a person
 * typed, not things the plan says, and saying so before a round trip is courtesy rather than
 * a rule.
 *
 * **Thresholds are never pre-filled.** The panel shows this plan's value beside each metric
 * and leaves the limit empty. A limit filled in from the measured value would make "a `no`
 * breaches its own bound" pass by construction, which is a check that cannot fail — the
 * failure ADR 0022 was written about.
 */

export type WouldRun = "yes" | "no" | "unsure";
export type Side = "max" | "min";

export interface MetricRow {
  name: string;
  meaning: string;
  value: number | null;
}

export interface BoundRow {
  metric: string;
  side: Side;
  /** As typed. A string, so an empty box is distinguishable from a zero. */
  limit: string;
}

export interface VerdictForm {
  wouldRun: WouldRun | null;
  reason: string;
  rows: BoundRow[];
  /** Only sent for a `no`, and only when the person says no number here says why. */
  useGap: boolean;
  gap: string;
  note: string;
  /** The golden this stored plan was seeded from, or "" for none. */
  golden: string;
}

export interface VerdictBody {
  would_run: WouldRun;
  reason: string;
  bounds: Record<string, number>;
  gap?: string;
  note?: string;
  golden?: string;
}

/** The server's floor, shown as a hint and not enforced here: it is the server's rule. */
export const REASON_WORDS_HINT = 12;

export function emptyForm(): VerdictForm {
  return { wouldRun: null, reason: "", rows: [], useGap: false, gap: "", note: "", golden: "" };
}

export function wordCount(text: string): number {
  return text.split(/\s+/).filter(Boolean).length;
}

/** Only a metric this plan has a value for can be bounded; the others are shown, not offered. */
export function boundable(metrics: MetricRow[]): MetricRow[] {
  return metrics.filter((metric) => metric.value !== null);
}

/** A new, empty row on the first boundable metric not already used. */
export function nextRow(metrics: MetricRow[], rows: BoundRow[]): BoundRow | null {
  const used = new Set(rows.map((row) => `${row.metric}_${row.side}`));
  for (const metric of boundable(metrics)) {
    for (const side of ["max", "min"] as const) {
      if (!used.has(`${metric.name}_${side}`)) return { metric: metric.name, side, limit: "" };
    }
  }
  return null;
}

/**
 * The request body, or what is wrong with the form.
 *
 * `problems` are about what was typed; every judgement about the route is the server's.
 */
export function bodyFrom(form: VerdictForm): { body: VerdictBody | null; problems: string[] } {
  const problems: string[] = [];
  if (form.wouldRun === null) problems.push("Choose yes, no or unsure.");

  const bounds: Record<string, number> = {};
  for (const row of form.rows) {
    const name = `${row.metric}_${row.side}`;
    const text = row.limit.trim();
    if (text === "") {
      problems.push(`Give ${name} a limit, or remove the row.`);
      continue;
    }
    const limit = Number(text);
    if (!Number.isFinite(limit)) {
      problems.push(`${name}: "${row.limit}" is not a number.`);
      continue;
    }
    if (name in bounds) {
      problems.push(`${name} is stated twice.`);
      continue;
    }
    bounds[name] = limit;
  }

  const gapSent = form.wouldRun === "no" && form.useGap;
  if (gapSent && form.gap.trim() === "") {
    problems.push("Say what no number here captures, or untick the box.");
  }

  if (problems.length > 0 || form.wouldRun === null) return { body: null, problems };
  const body: VerdictBody = { would_run: form.wouldRun, reason: form.reason.trim(), bounds };
  if (gapSent) body.gap = form.gap.trim();
  if (form.note.trim()) body.note = form.note.trim();
  if (form.golden) body.golden = form.golden;
  return { body, problems };
}

/** A number the way the panel prints it: absent is a dash, never a zero. */
export function shown(value: number | null): string {
  if (value === null) return "—";
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(3)));
}
