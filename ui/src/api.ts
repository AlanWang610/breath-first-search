/**
 * The API client, and the types it returns (scope 10.3).
 *
 * These types are hand-written against the plan schema rather than generated from
 * `/openapi.json`, and they are deliberately **partial**: a field the UI does not draw is
 * not declared here. That is not laziness, it is the one thing that keeps this file from
 * becoming a second schema — the API's contract is `Plan` as pydantic dumps it, and a
 * complete TypeScript mirror would drift the first time a scorer added a key.
 *
 * Three fields below carry `| null` where a lazier client would use a number, and every one
 * of them is a place the backend deliberately reports "nobody measured" rather than a
 * plausible default. Rendering `null` as `0` in a chart would undo the work `core/` does to
 * keep unknown and absent apart (scope 3.6), so `null` is threaded all the way to the
 * label.
 */

export interface Health {
  ok: boolean;
  version: string;
  fixtures: string;
  fixtures_present: boolean;
  router: string | null;
  offline: boolean;
}

export interface RoutePoint {
  lat: number;
  lon: number;
  ele_m: number | null;
  cum_dist_m: number;
}

export interface Flag {
  scorer: string;
  segment_id: string;
  kind: "soft" | "hard";
  tier: string;
  severity: number;
  reason_code: string;
  detail?: string | null;
}

export interface Segment {
  id: string;
  index: number;
  start_idx: number;
  end_idx: number;
  cum_start_m: number;
  length_m: number;
  way_id: number | null;
}

export interface CoverageEntry {
  source: string;
  kind: string;
  checked: boolean;
  reason?: string | null;
  jurisdiction?: string | null;
  tier?: number | null;
}

export interface Metrics {
  length_m: number;
  fraction_lts3_plus: number | null;
  lts4_count: number | null;
  shortest_legal_m: number | null;
  detour_ratio: number | null;
  reasons: string[];
}

export interface TradeOff {
  segment_id: string;
  options: string[];
  comparison: string;
}

export interface Plan {
  id: string;
  status: string;
  route: { id: string; points: RoutePoint[]; length_m?: number };
  segments: Segment[];
  results: { name: string; flags: Flag[] }[];
  etas: string[];
  residual_flags: Flag[];
  trade_offs: TradeOff[];
  warnings: string[];
  pacing_caveats: string[];
  coverage: { entries: CoverageEntry[] };
  metrics: Partial<Metrics>;
  elevation: { gain_m: number; loss_m: number; has_elevation: boolean } | null;
  profile: Record<string, { value: unknown; provenance: string; weight: number }>;
}

export interface PlanSummary {
  plan_id: string;
  id: string | null;
  date: string | null;
  length_m: number | null;
  status: string | null;
  trade_offs: number;
}

export interface PendingQuestion {
  id: string;
  kind: string;
  prompt: string;
  options: string[];
  segment_id?: string | null;
}

export interface JobView {
  job_id: string;
  status: string;
  events: { kind: string; message: string; at: string }[];
  question: PendingQuestion | null;
  plan_id: string | null;
}

export interface Region {
  name: string;
  built: boolean;
  steps?: Record<string, boolean>;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    // The API puts a sentence in `detail` for every refusal it makes on purpose — an
    // answer that was not on offer, a job that is not waiting. Surfacing that instead of
    // "Request failed" is the difference between a UI that explains and one that shrugs.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* a non-JSON error body is still an error; keep the status line */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => call<Health>("/api/health"),
  profile: () => call<Plan["profile"]>("/api/profile"),
  plans: () => call<PlanSummary[]>("/api/plans"),
  plan: (id: string) => call<Plan>(`/api/plans/${encodeURIComponent(id)}`),
  regions: () => call<Region[]>("/api/regions"),
  jobs: () => call<{ job_id: string; status: string }[]>("/api/jobs"),
  job: (id: string) => call<JobView>(`/api/jobs/${encodeURIComponent(id)}`),
  submit: (body: unknown) =>
    call<JobView>("/api/plans", { method: "POST", body: JSON.stringify(body) }),
  resume: (id: string, choice: string) =>
    call<JobView>(`/api/jobs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
      body: JSON.stringify({ choice }),
    }),
};

/** Flags by segment id, worst first — what the map colours a segment by. */
export function flagsBySegment(plan: Plan): Map<string, Flag[]> {
  const out = new Map<string, Flag[]>();
  for (const result of plan.results) {
    for (const flag of result.flags) {
      const list = out.get(flag.segment_id) ?? [];
      list.push(flag);
      out.set(flag.segment_id, list);
    }
  }
  for (const list of out.values()) {
    list.sort((a, b) => (a.kind === b.kind ? b.severity - a.severity : a.kind === "hard" ? -1 : 1));
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
 */
export function tierColour(tier: string): string {
  switch (tier) {
    case "safety":
      return "#d1495b";
    case "physiological":
      return "#e8a33d";
    default:
      return "#4a8fc2";
  }
}
