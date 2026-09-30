/**
 * "Would you run this?" — a person's verdict on the plan on the screen (ADR 0048).
 *
 * The one panel on this page that records something about a *person* rather than a route,
 * and so the one whose rules are the strictest about where they live:
 *
 * - **The server decides whether a verdict stands.** Every refusal shown here is
 *   `check_verdict`'s own sentence, one per line; `lib/verdict` checks only what was typed.
 * - **Limits start empty.** Each metric's value for this plan is shown beside the box and
 *   never copied into it: a limit pre-filled from the plan makes a `no` breach itself by
 *   construction, which is a check that cannot fail.
 * - **Only numbers this plan has can be bounded.** The rest are listed, greyed, so it is
 *   visible what the scorers did not measure here — which is also what `gap` is for.
 */
import { useEffect, useState } from "react";
import { api, type Measured, type VerdictReceipt } from "../api";
import {
  REASON_WORDS_HINT,
  bodyFrom,
  boundable,
  emptyForm,
  nextRow,
  shown,
  wordCount,
  type BoundRow,
  type Side,
  type VerdictForm,
  type WouldRun,
} from "../lib/verdict";

const ANSWERS: WouldRun[] = ["yes", "no", "unsure"];

export function JudgePanel({
  planId,
  measured,
  onRecorded,
}: {
  planId: string;
  measured: Measured;
  onRecorded: (receipt: VerdictReceipt) => void;
}) {
  const [form, setForm] = useState<VerdictForm>(emptyForm);
  const [problems, setProblems] = useState<string[]>([]);
  const [receipt, setReceipt] = useState<VerdictReceipt | null>(null);
  const [sending, setSending] = useState(false);

  // A different plan is a different judgement: nothing typed about one carries to the next.
  useEffect(() => {
    setForm(emptyForm());
    setProblems([]);
    setReceipt(null);
  }, [planId]);

  const update = (patch: Partial<VerdictForm>) => setForm((current) => ({ ...current, ...patch }));
  const setRow = (index: number, patch: Partial<BoundRow>) =>
    update({ rows: form.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)) });
  const valueOf = (metric: string) =>
    measured.metrics.find((row) => row.name === metric)?.value ?? null;
  const words = wordCount(form.reason);
  const candidate = nextRow(measured.metrics, form.rows);

  async function submit() {
    const { body, problems: typed } = bodyFrom(form);
    setReceipt(null);
    if (!body) {
      setProblems(typed);
      return;
    }
    setSending(true);
    try {
      const recorded = await api.verdict(planId, body);
      setProblems([]);
      setReceipt(recorded);
      onRecorded(recorded);
    } catch (e) {
      setProblems((e as Error).message.split("\n"));
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="panel judge">
      <h2>Would you run this?</h2>
      <p className="small">
        <a href={api.sheetUrl(planId)} target="_blank" rel="noreferrer">
          Read the plan sheet
        </a>{" "}
        <span className="muted">first — heat, water and ways out are laid out along the route.</span>
      </p>
      {measured.labeler === null && (
        <p className="bad small">
          This server was started without LONGRUN_LABELER, so it cannot say whose verdict this
          is. Set it and restart <code>longrun api</code>.
        </p>
      )}
      {measured.judged.length > 0 && (
        <p className="muted small">
          You judged this plan before:{" "}
          {measured.judged
            .map((j) => `${j.would_run} (${j.labelled_at.slice(0, 10)}, ${j.via})`)
            .join(", ")}
          . A new verdict is added, not replaced.
        </p>
      )}

      <div className="row" role="radiogroup" aria-label="Would you run this?">
        {ANSWERS.map((answer) => (
          <label key={answer} className="choice">
            <input
              type="radio"
              name="would-run"
              value={answer}
              checked={form.wouldRun === answer}
              onChange={() => update({ wouldRun: answer })}
            />{" "}
            {answer}
          </label>
        ))}
      </div>

      <label className="stack">
        Why{" "}
        <span className={words >= REASON_WORDS_HINT ? "muted small" : "small"}>
          ({words} word{words === 1 ? "" : "s"}; a sentence of {REASON_WORDS_HINT}+)
        </span>
        <textarea
          aria-label="Why"
          rows={3}
          value={form.reason}
          onChange={(e) => update({ reason: e.target.value })}
        />
      </label>

      <h3 className="small">Bounds — the envelope you would accept</h3>
      <p className="muted small">
        {form.wouldRun === "no"
          ? "For a no, set the limit you would accept on the number that is wrong; this route's value should fall outside it."
          : "The limits this route sits inside and a route you would run should too."}
      </p>
      {form.rows.map((row, index) => (
        <div className="row bound" key={index}>
          <select
            aria-label={`bound ${index + 1} metric`}
            value={row.metric}
            onChange={(e) => setRow(index, { metric: e.target.value })}
          >
            {boundable(measured.metrics).map((metric) => (
              <option key={metric.name} value={metric.name}>
                {metric.name}
              </option>
            ))}
          </select>
          <select
            aria-label={`bound ${index + 1} side`}
            value={row.side}
            onChange={(e) => setRow(index, { side: e.target.value as Side })}
          >
            <option value="max">at most</option>
            <option value="min">at least</option>
          </select>
          <input
            aria-label={`bound ${index + 1} limit`}
            inputMode="decimal"
            placeholder="limit"
            value={row.limit}
            onChange={(e) => setRow(index, { limit: e.target.value })}
          />
          <span className="muted small">this route: {shown(valueOf(row.metric))}</span>
          <button onClick={() => update({ rows: form.rows.filter((_, i) => i !== index) })}>
            Remove
          </button>
        </div>
      ))}
      <div className="row">
        <button disabled={!candidate} onClick={() => candidate && update({ rows: [...form.rows, candidate] })}>
          Add a bound
        </button>
      </div>

      {form.wouldRun === "no" && (
        <label className="stack">
          <span>
            <input
              type="checkbox"
              checked={form.useGap}
              onChange={(e) => update({ useGap: e.target.checked })}
            />{" "}
            No number here says why
          </span>
          {form.useGap && (
            <input
              aria-label="What nothing measures"
              placeholder="what made it a no, that nothing here measures"
              value={form.gap}
              onChange={(e) => update({ gap: e.target.value })}
            />
          )}
        </label>
      )}

      <label className="stack">
        Seeded from a golden?
        <select
          aria-label="Golden"
          value={form.golden}
          onChange={(e) => update({ golden: e.target.value })}
        >
          <option value="">no</option>
          {measured.goldens.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>

      <label className="stack">
        Note
        <input aria-label="Note" value={form.note} onChange={(e) => update({ note: e.target.value })} />
      </label>

      <div className="row">
        <button onClick={submit} disabled={sending || measured.labeler === null}>
          {sending ? "recording…" : "Record verdict"}
        </button>
      </div>

      {problems.length > 0 && (
        <ul className="problems bad small" aria-label="Not recorded">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {receipt && (
        <p className="small" role="status">
          Recorded verdict {receipt.id} ({receipt.would_run}, by {receipt.labeler}).{" "}
          {receipt.publishable
            ? "Run `longrun label publish` and review the diff to put it in tests/eval."
            : "No golden, so it stays in your ledger."}
        </p>
      )}
    </section>
  );
}
