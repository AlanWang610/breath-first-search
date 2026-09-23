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

/**
 * A same-tier conflict the loop refused to resolve (scope 8.4, ADR 0019).
 *
 * `option_a` / `option_b`, which is what `core/models/plan.py` has always dumped. This
 * declared `options: string[]` from M7 until M12 and nothing noticed, because these types
 * are hand-written and `tsc` checks them against each other rather than against the schema
 * — so `trade.options.map(...)` type-checked and threw `Cannot read properties of
 * undefined` the moment a plan with a trade-off was opened, taking the panel and every
 * sibling down with it. Vitest is what would have caught it, and M12.8 is why there is any.
 */
export interface TradeOff {
  segment_id: string;
  option_a: string;
  option_b: string;
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

/**
 * The raster source drawn under the route (ADR 0023), or why there is none.
 *
 * `provider: null` is a normal answer — tiles switched off, or a misconfigured custom
 * provider — and the map draws the route on a blank ground either way.
 */
export interface Basemap {
  provider: string | null;
  reason?: string;
  kind?: "imagery" | "map";
  tiles?: string[];
  tile_size?: number;
  maxzoom?: number;
  attribution?: string;
  licence?: string;
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
  basemap: () => call<Basemap>("/api/basemap"),
  jobs: () => call<{ job_id: string; status: string }[]>("/api/jobs"),
  job: (id: string) => call<JobView>(`/api/jobs/${encodeURIComponent(id)}`),
  submit: (body: unknown) =>
    call<JobView>("/api/plans", { method: "POST", body: JSON.stringify(body) }),
  resume: (id: string, choice: string) =>
    call<JobView>(`/api/jobs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
      body: JSON.stringify({ choice }),
    }),
  /**
   * The five gestures (scope 10.3), each one a POST to a plan **id**.
   *
   * No path, ever: `tools/` is file-path-parameterised throughout and the same convention
   * with write semantics over HTTP is a different thing entirely (ADR 0036). So `choose`
   * sends the alternative as points and not as a GPX filename, and there is nothing on
   * this object that names a file.
   *
   * Each returns a `JobView`, because each is a job. A lock is instantaneous and a choose
   * is a full re-score and they poll identically, which is the whole reason they are the
   * same shape.
   */
  edit: {
    lock: (id: string, body: { start_m: number; end_m: number; reason?: string }) =>
      write(id, "lock", body),
    unlock: (id: string, body: { start_m: number; end_m: number; mine?: boolean }) =>
      write(id, "unlock", body),
    via: (id: string, body: { at: string; at_m?: number }) => write(id, "via", body),
    avoid: (id: string, body: { polygon: unknown }) => write(id, "avoid", body),
    choose: (
      id: string,
      body: { start_m: number; end_m: number; alternative: string[]; only?: string[] },
    ) => write(id, "choose", body),
  },
};

function write(planId: string, gesture: string, body: unknown): Promise<JobView> {
  return call<JobView>(`/api/plans/${encodeURIComponent(planId)}/${gesture}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// `flagsBySegment` and `tierColour` moved to `lib/plan.ts` in M12.8, with the rest of the
// functions a headless run can check. This file is the client and the types it returns;
// anything that is a function of a `Plan` and nothing else belongs where the tests are.
