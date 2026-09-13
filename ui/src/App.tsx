/**
 * The web UI (scope 10.3), which adds no capability.
 *
 * Every control here calls an endpoint that wraps something `longrun` already does from a
 * shell — that is scope 3.9's rule and the reason this milestone is last. The value the UI
 * adds is *spatial review*: chat is poor at it and the HTML plan sheet is read-only.
 *
 * Two behaviours are worth knowing about before reading the code.
 *
 * **A parked job is a normal state.** When the loop hits a same-tier conflict it stops and
 * asks, and this renders the question with its options rather than an error. The user
 * chooses; the model, where there is one, only wrote the comparison (ADR 0019).
 *
 * **Polling, not a socket.** A plan is seconds to a couple of minutes and the event list is
 * tens of entries, so a 1.5 s poll keeps the server a plain request/response surface that a
 * curl drives as well as a browser. Worth revisiting only if a plan ever streams geometry.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { MapView } from "./components/MapView";
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

const POLL_MS = 1500;

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [regions, setRegions] = useState<Region[]>([]);
  const [profile, setProfile] = useState<Plan["profile"] | null>(null);
  const [summaries, setSummaries] = useState<PlanSummary[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [job, setJob] = useState<JobView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [start, setStart] = useState("37.7955,-122.3937");
  const [end, setEnd] = useState("37.7715,-122.4686");
  const [date, setDate] = useState("2026-09-15");
  const [startTime, setStartTime] = useState("08:00");

  const timer = useRef<number | null>(null);

  const refreshPlans = useCallback(() => {
    api.plans().then(setSummaries).catch(() => setSummaries([]));
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch((e: Error) => setError(e.message));
    api.regions().then(setRegions).catch(() => setRegions([]));
    api.profile().then(setProfile).catch(() => setProfile(null));
    refreshPlans();
  }, [refreshPlans]);

  const openPlan = useCallback((planId: string) => {
    setError(null);
    api.plan(planId).then(setPlan).catch((e: Error) => setError(e.message));
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

  useEffect(() => () => {
    if (timer.current !== null) window.clearInterval(timer.current);
  }, []);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      const view = await api.submit({
        start,
        end,
        date,
        start_time: startTime,
      });
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
          <MapView plan={plan} />
          <Timeline plan={plan} />
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
