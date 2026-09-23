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
 *
 * **M12 made that last sentence load-bearing rather than cautionary.** Until this milestone
 * every route reaching this strip had been sampled end to end, so the profile was one
 * unbroken polyline and the `ele_m === null` branch was for a route with no DEM at all.
 * `choose` produces something new: a line whose *middle* is unmeasured, because `splice`
 * keeps the heights of the ground it did not touch and leaves the replacement's unknown
 * (re-reading a DEM for the untouched two-thirds is M10.4's bug through a different door).
 * Filtering the nulls out and drawing through the rest — which is what this did — puts a
 * straight line across that stretch, and a straight line on an elevation chart reads as
 * flat ground. That is the same error as rendering `detour_ratio: null` as `1.0`, made with
 * a pen instead of a number. It is drawn as a break now, and the gap is named in metres,
 * because a reader should not have to measure a chart to find out how much of it is missing.
 *
 * **M12.7: the strip has a cursor and the map shows where it is.** It was a pure function of
 * `plan` with no handlers and no state shared with `MapView`, so "where on the ground is
 * this climb" had no answer short of counting kilometres by eye. The distance lives in
 * `App`, because two siblings cannot share state without their parent holding it, and the
 * map reads it as a marker.
 */
import { useRef } from "react";
import { FLAG_KIND, type Plan } from "../api";
import { elevationRuns, flagsBySegment, segmentRange, tierColour, unmeasuredMetres } from "../lib/plan";

const WIDTH = 1000;
const HEIGHT = 120;

export function Timeline({
  plan,
  cursorM,
  onCursor,
}: {
  plan: Plan | null;
  cursorM: number | null;
  onCursor: (metres: number | null) => void;
}) {
  const svg = useRef<SVGSVGElement | null>(null);
  if (!plan || plan.route.points.length < 2) return null;

  const points = plan.route.points;
  const total = points[points.length - 1].cum_dist_m || 1;
  const runs = elevationRuns(points);
  const elevations = points.map((p) => p.ele_m).filter((e): e is number => e !== null);
  const hasElevation = runs.length > 0;
  const low = hasElevation ? Math.min(...elevations) : 0;
  const high = hasElevation ? Math.max(...elevations) : 1;
  const span = high - low || 1;

  const x = (metres: number) => (metres / total) * WIDTH;
  const y = (metres: number) => HEIGHT - ((metres - low) / span) * (HEIGHT - 12) - 6;

  // One polyline per run of consecutive measured points, so an unmeasured stretch is a gap
  // in the line rather than a straight segment drawn across it.
  const profiles = runs.map((run) =>
    run.map((p) => `${x(p.cum_dist_m).toFixed(1)},${y(p.ele_m as number).toFixed(1)}`).join(" "),
  );
  const unmeasured = unmeasuredMetres(points);

  const flags = flagsBySegment(plan);
  const bars = plan.segments
    .map((segment) => {
      const worst = flags.get(segment.id)?.[0];
      if (!worst) return null;
      const { startM } = segmentRange(segment);
      return {
        id: segment.id,
        x: x(startM),
        width: Math.max(1.5, x(segment.length_m)),
        colour: tierColour(worst.tier),
        opacity: worst.kind === FLAG_KIND.HARD ? 0.95 : 0.5,
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

  function scrub(clientX: number) {
    const rect = svg.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const fraction = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    onCursor(fraction * total);
  }

  return (
    <section className="timeline">
      <header>
        <h2>Along the route</h2>
        <span className="muted">
          {(total / 1000).toFixed(1)} km
          {cursorM !== null ? ` · cursor at ${(cursorM / 1000).toFixed(2)} km` : ""}
        </span>
      </header>
      <svg
        ref={svg}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        onMouseMove={(event) => scrub(event.clientX)}
        onMouseLeave={() => onCursor(null)}
      >
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
        {profiles.map((profile, index) => (
          <polyline key={index} points={profile} fill="none" stroke="#dfe6ec" strokeWidth={1.5} />
        ))}
        {cursorM !== null && (
          <line
            x1={x(cursorM)}
            x2={x(cursorM)}
            y1={0}
            y2={HEIGHT}
            stroke="#f2f6f9"
            strokeWidth={1}
          />
        )}
      </svg>
      {unmeasured > 0 && (
        <p className="muted small">
          {(unmeasured / 1000).toFixed(2)} km of this line has no elevation reading — drawn as
          a break, not as flat ground. An edited stretch is unmeasured until something samples
          it.
        </p>
      )}
      {missing.length > 0 && (
        <p className="muted small">
          Not drawn, because this plan does not carry it: {missing.join(", ")}.
        </p>
      )}
    </section>
  );
}
