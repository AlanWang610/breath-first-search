/**
 * Review: which of two lines would you rather run? (ADR 0049)
 *
 * The page a preference pair is answered on. It shows what `core.labels.review.task_view`
 * lets it — two lines, their lengths, where they part — and nothing it does not: never which
 * vector drew a line, and no shares of stress, surface or path until the answer is in. The
 * server enforces both; this page has no way to show what it was not sent.
 *
 * Four answers, because two would force one. "No preference" is a real answer about two
 * routes a person has looked at, and "can't judge" is a real answer about not knowing the
 * ground; the server keeps both and makes a pair of neither. A choice carries a strength,
 * which becomes its weight in the fit. A first answer stands: a second look is a probe the
 * queue asks later, with A and B swapped, and this page does not say which ones they are.
 */
import { useCallback, useEffect, useState } from "react";
import { api, type LabelQueue, type ReviewTask } from "../api";
import {
  LINE_COLOURS,
  answerBody,
  describeAnswer,
  km,
  progress,
  shareRows,
  type Prefer,
  type Strength,
} from "../lib/review";
import { PairMap } from "./PairMap";

export function ReviewView() {
  const [queue, setQueue] = useState<LabelQueue | null>(null);
  const [task, setTask] = useState<ReviewTask | null>(null);
  const [strength, setStrength] = useState<Strength | null>(null);
  const [reason, setReason] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [sending, setSending] = useState(false);

  const loadNext = useCallback(async () => {
    setProblems([]);
    setStrength(null);
    setReason("");
    try {
      const current = await api.labels.queue();
      setQueue(current);
      setTask(current.next ? await api.labels.task(current.next) : null);
    } catch (e) {
      setProblems([(e as Error).message]);
    }
  }, []);

  useEffect(() => {
    void loadNext();
  }, [loadNext]);

  async function answer(prefer: Prefer) {
    if (!task) return;
    const { body, problems: typed } = answerBody(prefer, strength, reason);
    if (!body) {
      setProblems(typed);
      return;
    }
    setSending(true);
    try {
      setTask(await api.labels.answer(task.id, body));
      setProblems([]);
      setQueue(await api.labels.queue());
    } catch (e) {
      setProblems((e as Error).message.split("\n"));
    } finally {
      setSending(false);
    }
  }

  const answered = task?.answer ?? null;
  const locked = sending || answered !== null || queue?.labeler === null;

  return (
    <div className="layout">
      <aside className="side">
        <section className="panel review">
          <h2>Which would you rather run?</h2>
          {queue && <p className="muted small">{progress(queue)}</p>}
          {queue?.labeler === null && (
            <p className="bad small">
              This server was started without LONGRUN_LABELER, so it cannot say whose answer
              this is. Set it and restart <code>longrun api</code>.
            </p>
          )}

          {!task && queue && (
            <p className="small">
              Nothing left to ask. Queue more with{" "}
              <code>longrun label seed --bbox WEST,SOUTH,EAST,NORTH</code> while the region's
              router is running.
            </p>
          )}

          {task && (
            <>
              <ul className="legend">
                {(["a", "b"] as const).map((which) => (
                  <li key={which}>
                    <span className="swatch" style={{ background: LINE_COLOURS[which] }} />
                    <strong>{which.toUpperCase()}</strong> {km(task[which].length_m)}
                  </li>
                ))}
              </ul>
              <p className="muted small">
                Heavier stretches are where the two part. Nothing else about them is shown
                until you answer.
              </p>

              <div className="row" role="radiogroup" aria-label="How strongly">
                {(["slight", "clear"] as const).map((level) => (
                  <label key={level} className="choice">
                    <input
                      type="radio"
                      name="strength"
                      checked={strength === level}
                      disabled={locked}
                      onChange={() => setStrength(level)}
                    />{" "}
                    {level === "slight" ? "slightly" : "clearly"}
                  </label>
                ))}
              </div>
              <div className="row">
                <button disabled={locked} onClick={() => answer("a")}>
                  A
                </button>
                <button disabled={locked} onClick={() => answer("b")}>
                  B
                </button>
                <button disabled={locked} onClick={() => answer("none")}>
                  No preference
                </button>
                <button disabled={locked} onClick={() => answer("cant")}>
                  Can't judge
                </button>
              </div>
              <label className="stack">
                Why (optional)
                <input
                  aria-label="Why (optional)"
                  value={reason}
                  disabled={locked}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>

              {problems.length > 0 && (
                <ul className="problems bad small" aria-label="Not recorded">
                  {problems.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              )}

              {answered && task.shares && (
                <div role="status">
                  <p className="small">
                    Recorded: <strong>{describeAnswer(answered)}</strong>.
                  </p>
                  <table className="profile shares">
                    <thead>
                      <tr>
                        <th />
                        <th>A</th>
                        <th>B</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shareRows(task.shares).map((row) => (
                        <tr key={row.label}>
                          <td>{row.label}</td>
                          <td>{row.a}</td>
                          <td>{row.b}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="row">
                    <button onClick={() => void loadNext()}>Next question</button>
                  </div>
                </div>
              )}
            </>
          )}
        </section>
      </aside>
      <main className="main">
        <PairMap task={task} />
      </main>
    </div>
  );
}
