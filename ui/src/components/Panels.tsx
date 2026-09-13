/**
 * The read-only panels (scope 10.3): metrics, trade-offs, coverage, preferences, regions.
 *
 * Every one of these renders an absence as an absence. That is the whole reason they are
 * hand-written rather than a generic JSON viewer: the backend spends real effort keeping
 * "unknown" and "absent" apart — a `detour_ratio` of `null`, a coverage entry with
 * `checked: false` and a reason, a jurisdiction nobody had an adapter for — and a panel
 * that printed `0`, or omitted the row, would throw all of it away at the last step.
 */
import type { CoverageEntry, Plan, Region, TradeOff } from "../api";

function fmt(value: number | null | undefined, digits = 1, suffix = ""): string {
  return value === null || value === undefined ? "not measured" : `${value.toFixed(digits)}${suffix}`;
}

export function MetricsPanel({ plan }: { plan: Plan }) {
  const m = plan.metrics ?? {};
  const fraction = m.fraction_lts3_plus;
  return (
    <section className="panel">
      <h2>Acceptance metrics</h2>
      <dl className="kv">
        <dt>Length</dt>
        <dd>{fmt((plan.route.length_m ?? 0) / 1000, 2, " km")}</dd>
        <dt>At LTS 3 or worse</dt>
        <dd>
          {fraction === null || fraction === undefined
            ? "not measured"
            : `${(fraction * 100).toFixed(1)}%`}
        </dd>
        <dt>LTS 4 segments</dt>
        <dd>{m.lts4_count ?? "not measured"}</dd>
        <dt>Detour vs shortest legal</dt>
        {/* `null` is threaded all the way here on purpose. A 1.0 would read as "already
            as short as possible", which is a measurement nobody made. */}
        <dd>{m.detour_ratio === null || m.detour_ratio === undefined ? "not measured" : `${m.detour_ratio.toFixed(3)}×`}</dd>
      </dl>
      {(m.reasons ?? []).map((reason) => (
        <p key={reason} className="muted small">
          {reason}
        </p>
      ))}
    </section>
  );
}

export function TradeOffsPanel({
  tradeOffs,
  onChoose,
}: {
  tradeOffs: TradeOff[];
  onChoose?: (option: string) => void;
}) {
  if (tradeOffs.length === 0) return null;
  return (
    <section className="panel">
      <h2>Choices left to you</h2>
      {/* Scope 8.4 and ADR 0019: the loop never resolves a same-tier conflict on the
          runner's behalf, because a tier boundary is a claim of incommensurability. */}
      {tradeOffs.map((trade) => (
        <div key={trade.segment_id} className="tradeoff">
          <p>{trade.comparison}</p>
          <div className="row">
            {trade.options.map((option) => (
              <button key={option} onClick={() => onChoose?.(option)} disabled={!onChoose}>
                {option}
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

export function CoveragePanel({ entries }: { entries: CoverageEntry[] }) {
  const unchecked = entries.filter((entry) => !entry.checked);
  return (
    <section className="panel">
      <h2>
        Coverage <span className="muted">{entries.length - unchecked.length}/{entries.length} answered</span>
      </h2>
      {unchecked.length === 0 ? (
        <p className="muted small">Every source this plan asked, answered.</p>
      ) : (
        <ul className="coverage">
          {unchecked.map((entry, index) => (
            <li key={`${entry.source}-${index}`}>
              <strong>{entry.source}</strong>
              {entry.jurisdiction ? <span className="muted"> · {entry.jurisdiction}</span> : null}
              {entry.tier ? <span className="muted"> · tier {entry.tier}</span> : null}
              <div className="muted small">{entry.reason ?? "not checked"}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function WarningsPanel({ plan }: { plan: Plan }) {
  const all = [...(plan.pacing_caveats ?? []), ...(plan.warnings ?? [])];
  if (all.length === 0) return null;
  return (
    <section className="panel">
      <h2>Warnings</h2>
      <ul>
        {all.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
      </ul>
    </section>
  );
}

export function PreferencePanel({ profile }: { profile: Plan["profile"] | null }) {
  if (!profile) return null;
  const rows = Object.entries(profile).filter(
    ([, entry]) => entry && typeof entry === "object" && "provenance" in entry,
  );
  return (
    <section className="panel">
      <h2>Preferences</h2>
      {/* Provenance is the point, not a detail. Scope 6.3 permits an in-context question
          only about an axis that is still `default`, so a panel that hid it could not
          show the one distinction the rule turns on. */}
      <table className="profile">
        <tbody>
          {rows.map(([key, entry]) => (
            <tr key={key}>
              <td>{key.replace(/_/g, " ")}</td>
              <td className="mono">{JSON.stringify(entry.value)}</td>
              <td className={`prov prov-${entry.provenance}`}>{entry.provenance}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function RegionsPanel({ regions }: { regions: Region[] }) {
  if (regions.length === 0) return null;
  return (
    <section className="panel">
      <h2>Regions</h2>
      <ul className="regions">
        {regions.map((region) => {
          const steps = region.steps ?? {};
          const done = Object.values(steps).filter(Boolean).length;
          const total = Object.keys(steps).length;
          return (
            <li key={region.name}>
              <strong>{region.name}</strong>{" "}
              {/* "not built" and "does not exist" are different answers, so an unbuilt
                  region is listed rather than omitted. */}
              <span className="muted">
                {region.built ? `${done}/${total} steps` : "not built"}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
