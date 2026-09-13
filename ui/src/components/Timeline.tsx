/**
 * The timeline strip (scope 10.3): elevation and flags against distance.
 *
 * Scope 10.3 asks for elevation, ETA, temperature/WBGT, shade fraction, service gaps and
 * daylight, all aligned by distance. This draws the two the plan schema carries per point
 * and per segment — elevation and flags — and **says which of the others it could not
 * draw**, rather than leaving a reader to assume the route has no heat on it.
 *
 * That is scope 3.6 at the last surface it can still be lost at. The plan is careful to
 * report `unknown` rather than `absent` all the way from the scorer; a chart that silently
 * omits a missing series undoes that in one step.
 */
import type { Plan } from "../api";
import { flagsBySegment, tierColour } from "../api";

const WIDTH = 1000;
const HEIGHT = 120;

export function Timeline({ plan }: { plan: Plan | null }) {
  if (!plan || plan.route.points.length < 2) return null;

  const points = plan.route.points;
  const total = points[points.length - 1].cum_dist_m || 1;
  const elevations = points.map((p) => p.ele_m).filter((e): e is number => e !== null);
  const hasElevation = elevations.length > 1;
  const low = hasElevation ? Math.min(...elevations) : 0;
  const high = hasElevation ? Math.max(...elevations) : 1;
  const span = high - low || 1;

  const x = (metres: number) => (metres / total) * WIDTH;
  const y = (metres: number) => HEIGHT - ((metres - low) / span) * (HEIGHT - 12) - 6;

  const profile = hasElevation
    ? points
        .filter((p) => p.ele_m !== null)
        .map((p) => `${x(p.cum_dist_m).toFixed(1)},${y(p.ele_m as number).toFixed(1)}`)
        .join(" ")
    : "";

  const flags = flagsBySegment(plan);
  const bars = plan.segments
    .map((segment) => {
      const worst = flags.get(segment.id)?.[0];
      if (!worst) return null;
      return {
        id: segment.id,
        x: x(segment.cum_start_m),
        width: Math.max(1.5, x(segment.length_m)),
        colour: tierColour(worst.tier),
        opacity: worst.kind === "hard" ? 0.95 : 0.5,
      };
    })
    .filter((bar): bar is NonNullable<typeof bar> => bar !== null);

  // Named rather than inferred: a series the plan does not carry is one this strip cannot
  // draw, and the honest thing is to say so where the chart would have been.
  const missing: string[] = [];
  if (!hasElevation) missing.push("elevation");
  const scorers = new Set(plan.results.map((r) => r.name));
  if (!scorers.has("heat_stress")) missing.push("WBGT");
  if (!scorers.has("sun_exposure")) missing.push("shade");
  if (!scorers.has("resupply_schedule")) missing.push("service gaps");
  if (!scorers.has("lighting")) missing.push("daylight");
  if (plan.etas.length === 0) missing.push("ETA");

  return (
    <section className="timeline">
      <header>
        <h2>Along the route</h2>
        <span className="muted">{(total / 1000).toFixed(1)} km</span>
      </header>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none" role="img">
        <title>Elevation and flagged segments against distance</title>
        {bars.map((bar) => (
          <rect
            key={bar.id}
            x={bar.x}
            y={0}
            width={bar.width}
            height={HEIGHT}
            fill={bar.colour}
            opacity={bar.opacity}
          />
        ))}
        {hasElevation && (
          <polyline points={profile} fill="none" stroke="#dfe6ec" strokeWidth={1.5} />
        )}
      </svg>
      {missing.length > 0 && (
        <p className="muted small">
          Not drawn, because this plan does not carry it: {missing.join(", ")}.
        </p>
      )}
    </section>
  );
}
