/**
 * The web UI (scope 10.3), which adds no capability.
 *
 * Every control here calls an endpoint that wraps something `longrun` already does from a
 * shell — that is scope 3.9's rule and the reason this milestone is last. The value the UI
 * adds is *spatial review*: chat is poor at it and the HTML plan sheet is read-only.
 *
 * Three behaviours are worth knowing about before reading the code.
 *
 * **A parked job is a normal state.** When the loop hits a same-tier conflict it stops and
 * asks, and this renders the question with its options rather than an error. The user
 * chooses; the model, where there is one, only wrote the comparison (ADR 0019).
 *
 * **Polling, not a socket.** A plan is seconds to a couple of minutes and the event list is
 * tens of entries, so a 1.5 s poll keeps the server a plain request/response surface that a
 * curl drives as well as a browser. Worth revisiting only if a plan ever streams geometry.
 *
 * **An edit is a job like any other** (M12.2). A lock is instantaneous and a replaced
 * stretch is a full re-score, and both go out as a POST that returns a job id and come back
 * through the same poll. So this file gained one code path, not five, and the plan is
 * reloaded from disk when the job ends rather than patched in memory — the server is the
 * only thing that knows what an edit did to the measurements.
 *
 * **What this file does not hold is a second copy of any rule.** The distances a gesture
 * sends come from `lib/plan`, which is arithmetic the plan already carries; the polygon it
 * sends is unrounded, because rounding at `AREA_PRECISION` has one owner and it is on the
 * server. Every refusal shown below is the server's sentence, verbatim.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { MapView, type DrawMode, type Selection } from "./components/MapView";
import { Timeline } from "./components/Timeline";
import {
  CoveragePanel,
  MetricsPanel,
  PreferencePanel,
  RegionsPanel,
  TradeOffsPanel,
  WarningsPanel,
} from "./components/Panels";
import { api } from "./api";
import type { Health, JobView, Plan, PlanSummary, Region } from "./api";
import { lineFromVertices, polygonFromVertices, type Vertex } from "./lib/plan";

const POLL_MS = 1500;

/**
 * Why a via or an avoid area leaves the line alone, said where the button is rather than
 * only in an event log.
 *
 * `RoutingPolicy` is frozen and resolved once, persisted so that a resume in another
 * process cannot compute a different one. Patching an avoid area into the policy the first
 * half of a line was already drawn under would cost that invariant and buy a line that is
 * half one thing and half another. The honest option is a whole re-route, which is a
 * routing call — and there is no router behind this page.
 */
const FROZEN_POLICY_NOTE =
  "A via point and an avoid area change the request, not the line: the stored route was " +
  "drawn under a routing policy frozen when the plan began. Re-routing under the new one " +
  "is a routing call — run `longrun edit reroute` against the stored plan.";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [regions, setRegions] = useState<Region[]>([]);
  const [profile, setProfile] = useState<Plan["profile"] | null>(null);
  const [summaries, setSummaries] = useState<PlanSummary[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planId, setPlanId] = useState<string | null>(null);
  const [job, setJob] = useState<JobView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [start, setStart] = useState("37.7955,-122.3937");
  const [end, setEnd] = useState("37.7715,-122.4686");
  const [date, setDate] = useState("2026-09-15");
  const [startTime, setStartTime] = useState("08:00");

  // Direct manipulation (scope 10.3). `selection` is a pair of distances and never a
  // segment id: `segment_id` is positional, so an id read before an edit names different
  // ground after one (M11.3), and the write endpoints take metres for that reason.
  const [drawMode, setDrawMode] = useState<DrawMode>(null);
  const [vertices, setVertices] = useState<Vertex[]>([]);
  const [selection, setSelection] = useState<(Selection & { id: string }) | null>(null);
  const [cursorM, setCursorM] = useState<number | null>(null);

  const timer = useRef<number | null>(null);

  const refreshPlans = useCallback(() => {
    api
      .plans()
      .then(setSummaries)
      .catch(() => setSummaries([]));
  }, []);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
    api
      .regions()
      .then(setRegions)
      .catch(() => setRegions([]));
    api
      .profile()
      .then(setProfile)
      .catch(() => setProfile(null));
    refreshPlans();
  }, [refreshPlans]);

  const openPlan = useCallback((id: string) => {
    setError(null);
    setPlanId(id);
    api
      .plan(id)
      .then(setPlan)
      .catch((e: Error) => setError(e.message));
  }, []);

  const poll = useCallback(
    (jobId: string) => {
      if (timer.current !== null) window.clearInterval(timer.current);
      timer.current = window.setInterval(async () => {
        try {
          const view = await api.job(jobId);
          setJob(view);
          if (view.status !== "running") {
            window.clearInterval(timer.current!);
            timer.current = null;
            setBusy(false);
            refreshPlans();
            if (view.plan_id) openPlan(view.plan_id);
          }
        } catch (e) {
          window.clearInterval(timer.current!);
          timer.current = null;
          setBusy(false);
          setError((e as Error).message);
        }
      }, POLL_MS);
    },
    [openPlan, refreshPlans],
  );

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearInterval(timer.current);
    },
    [],
  );

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      const view = await api.submit({ start, end, date, start_time: startTime });
      setJob(view);
      poll(view.job_id);
    } catch (e) {
      setBusy(false);
      setError((e as Error).message);
    }
  }

  async function choose(option: string) {
    if (!job) return;
    setError(null);
    setBusy(true);
    try {
      const view = await api.resume(job.job_id, option);
      setJob(view);
      poll(view.job_id);
    } catch (e) {
      setBusy(false);
      setError((e as Error).message);
    }
  }

  /**
   * One path for all five gestures.
   *
   * Every refusal the server makes on purpose — a range with no length, a polygon over the
   * cap with its size in square kilometres — arrives as `detail` and is shown as written.
   * A UI that replaced "the polygon covers 11.4 km2, larger than the 4 km2 cap" with
   * "invalid request" would take a sentence a runner can act on and return one they cannot.
   */
  async function edit(send: (id: string) => Promise<JobView>) {
    if (!planId) return;
    setError(null);
    setBusy(true);
    setDrawMode(null);
    setVertices([]);
    try {
      const view = await send(planId);
      setJob(view);
      poll(view.job_id);
    } catch (e) {
      setBusy(false);
      setError((e as Error).message);
    }
  }

  function onVertex(vertex: Vertex) {
    if (drawMode === "via") {
      void edit((id) => api.edit.via(id, { at: `${vertex.lat},${vertex.lng}` }));
      return;
    }
    setVertices((current) => [...current, vertex]);
  }

  function useDrawnArea() {
    const polygon = polygonFromVertices(vertices);
    if (!polygon) {
      setError("An avoid area needs at least three corners.");
      return;
    }
    void edit((id) => api.edit.avoid(id, { polygon }));
  }

  function useDrawnLine() {
    const alternative = lineFromVertices(vertices);
    if (!alternative || !selection) {
      setError("Pick a stretch on the map, then click along the line you want instead.");
      return;
    }
    void edit((id) =>
      api.edit.choose(id, {
        start_m: selection.startM,
        end_m: selection.endM,
        alternative,
      }),
    );
  }

  return (
    <div className="app">
      <header className="top">
        <h1>longrun</h1>
        {health && (
          <span className="muted small">
            v{health.version} · router {health.router ?? "not configured"} · fixtures{" "}
            {health.fixtures_present ? health.fixtures : `${health.fixtures} (missing)`}
            {health.offline ? " · offline" : ""}
          </span>
        )}
      </header>

      {error && <div className="error">{error}</div>}

      <div className="layout">
        <aside className="side">
          <section className="panel">
            <h2>Plan a route</h2>
            <label>
              From <input value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label>
              To <input value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
            <label>
              Date <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
            </label>
            <label>
              Start{" "}
              <input type="time" value={startTime} onChange={(e) => setStartTime(e.target.value)} />
            </label>
            <button onClick={submit} disabled={busy}>
              {busy ? "planning…" : "Plan"}
            </button>
          </section>

          {plan && planId && (
            <section className="panel">
              <h2>Edit this plan</h2>
              {selection ? (
                <p className="small">
                  {selection.id} · {(selection.startM / 1000).toFixed(2)}–
                  {(selection.endM / 1000).toFixed(2)} km
                </p>
              ) : (
                <p className="muted small">Click a segment on the map to select a stretch.</p>
              )}
              <div className="row">
                <button
                  disabled={busy || !selection}
                  onClick={() =>
                    selection &&
                    void edit((id) =>
                      api.edit.lock(id, {
                        start_m: selection.startM,
                        end_m: selection.endM,
                        reason: "locked on the map",
                      }),
                    )
                  }
                >
                  Lock
                </button>
                <button
                  disabled={busy || !selection}
                  onClick={() =>
                    selection &&
                    void edit((id) =>
                      api.edit.unlock(id, {
                        start_m: selection.startM,
                        end_m: selection.endM,
                        mine: true,
                      }),
                    )
                  }
                  title="Releases locks you set, leaving the loop's own reroute locks standing."
                >
                  Unlock mine
                </button>
                <button
                  disabled={busy || !selection}
                  onClick={() => {
                    setVertices([]);
                    setDrawMode("alternative");
                  }}
                >
                  Replace this stretch
                </button>
              </div>
              <div className="row">
                <button
                  disabled={busy}
                  onClick={() => {
                    setVertices([]);
                    setDrawMode("via");
                  }}
                >
                  Add a via
                </button>
                <button
                  disabled={busy}
                  onClick={() => {
                    setVertices([]);
                    setDrawMode("avoid");
                  }}
                >
                  Draw an avoid area
                </button>
                {drawMode && (
                  <button
                    onClick={() => {
                      setDrawMode(null);
                      setVertices([]);
                    }}
                  >
                    Cancel
                  </button>
                )}
              </div>
              {drawMode === "avoid" && (
                <div className="row">
                  <button disabled={busy || vertices.length < 3} onClick={useDrawnArea}>
                    Use this area ({vertices.length})
                  </button>
                </div>
              )}
              {drawMode === "alternative" && (
                <div className="row">
                  <button disabled={busy || vertices.length < 2} onClick={useDrawnLine}>
                    Use this line ({vertices.length})
                  </button>
                </div>
              )}
              {/* Said at the button, not only in the event log the server writes. */}
              <p className="muted small">{FROZEN_POLICY_NOTE}</p>
            </section>
          )}

          {job && (
            <section className="panel">
              <h2>
                Job <span className="muted">{job.status}</span>
              </h2>
              {job.question && (
                <div className="tradeoff">
                  {/* Not an error state. The loop stopped because two candidates are in
                      the same tier and only the runner can break that. */}
                  <p>{job.question.prompt}</p>
                  <div className="row">
                    {job.question.options.map((option) => (
                      <button key={option} onClick={() => choose(option)} disabled={busy}>
                        {option}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <ol className="events">
                {job.events.slice(-8).map((event, index) => (
                  <li key={index} className={event.kind === "failed" ? "bad" : undefined}>
                    {event.message}
                  </li>
                ))}
              </ol>
            </section>
          )}

          <section className="panel">
            <h2>Stored plans</h2>
            {summaries.length === 0 ? (
              <p className="muted small">Nothing planned yet.</p>
            ) : (
              <ul className="plans">
                {summaries.map((summary) => (
                  <li key={summary.plan_id}>
                    <button className="link" onClick={() => openPlan(summary.plan_id)}>
                      {summary.plan_id}
                    </button>{" "}
                    <span className="muted small">
                      {summary.date ?? "—"} ·{" "}
                      {summary.length_m ? `${(summary.length_m / 1000).toFixed(1)} km` : "—"}
                      {summary.trade_offs > 0 ? ` · ${summary.trade_offs} to decide` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <RegionsPanel regions={regions} />
          <PreferencePanel profile={plan?.profile ?? profile} />
        </aside>

        <main className="main">
          <MapView
            plan={plan}
            drawMode={drawMode}
            vertices={vertices}
            selection={selection}
            cursorM={cursorM}
            onPickSegment={(segment) => setSelection(segment)}
            onVertex={onVertex}
          />
          <Timeline plan={plan} cursorM={cursorM} onCursor={setCursorM} />
          {plan && (
            <div className="panels">
              <MetricsPanel plan={plan} />
              <TradeOffsPanel tradeOffs={plan.trade_offs ?? []} />
              <WarningsPanel plan={plan} />
              <CoveragePanel entries={plan.coverage?.entries ?? []} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
