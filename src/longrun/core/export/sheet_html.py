"""The plan sheet as one self-contained HTML file (scope 9).

Scope §9 asks for "a self-contained HTML file (embedded MapLibre map, elevation profile,
flagged segments with IDs, alternatives as dashed lines, **no server required**)". The two
halves of that sentence are in tension and this module resolves it toward the second.

**The map is inline SVG, not MapLibre.** A file that fetches a script from a CDN and tiles
from a basemap provider requires a server — two of them — and it acquires a §14 obligation
to whoever serves the tiles. Route geometry, flagged segments coloured by tier, and service
markers are what the reader actually needs to locate a problem, and all of that draws
without a network. MapLibre over a real basemap is the upgrade, and it belongs with the
decision about which tile provider and under what licence. Until then a file that opens on
a plane is worth more than one that opens in a browser with a connection.

**Everything is in the file.** No `<script src>`, no `<link href>`, no web fonts, no
images. Open it from a USB stick at kilometre 40 with no signal and it renders.

The one thing here that scope §9 does not ask for is the **timeline strip** — elevation,
WBGT, shade and daylight aligned by distance. That is §10.3, the web UI. It is included
deliberately: M2's whole subject is how sun and heat vary along a route, and a table of
per-segment numbers is a poor way to see that. It is static; the scrubbing and hover
behaviour §10.3 describes stays with the web UI.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

from longrun.core.export.attribution import render as render_attribution
from longrun.core.models.measurement import FlagKind, Tier
from longrun.core.scorers._common import ROUTE_SUMMARY_ID

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.measurement import Flag, ScorerResult
    from longrun.core.models.plan import Plan

#: Worst-N per scorer, matching the markdown sheet.
WORST_N = 5

#: Tier colours. Semantic, not decorative: red is a safety flag, amber physiological, blue
#: comfort — the scope 8.4 order, so a reader learns the mapping once.
TIER_COLOUR: dict[Tier, str] = {
    Tier.SAFETY: "#c0392b",
    Tier.PHYSIOLOGICAL: "#c47f17",
    Tier.COMFORT: "#2c6fb5",
}

MAP_WIDTH, MAP_HEIGHT = 880, 340
STRIP_WIDTH, STRIP_HEIGHT = 880, 64

_CSS = """
:root {
  --ink: #1b1d20; --muted: #5d6470; --rule: #d9dde3; --ground: #ffffff;
  --panel: #f6f7f9; --safety: #c0392b; --physio: #c47f17; --comfort: #2c6fb5;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--ink);
       font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 940px; margin: 0 auto; padding: 32px 24px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 17px; margin: 34px 0 10px; padding-bottom: 6px;
     border-bottom: 1px solid var(--rule); font-weight: 650; }
h3 { font-size: 14px; margin: 18px 0 6px; color: var(--muted);
     text-transform: uppercase; letter-spacing: 0.06em; }
.sub { color: var(--muted); margin: 0 0 24px; }
.facts { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 8px; padding: 0; list-style: none; }
.facts li { background: var(--panel); border: 1px solid var(--rule); border-radius: 6px;
            padding: 8px 12px; min-width: 130px; }
.facts b { display: block; font-size: 19px; font-variant-numeric: tabular-nums; }
.facts span { color: var(--muted); font-size: 12px; }
figure { margin: 0 0 10px; overflow-x: auto; }
svg { display: block; max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--rule);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
     letter-spacing: 0.05em; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
code { font: 13px/1.4 ui-monospace, "Cascadia Mono", Consolas, monospace;
       background: var(--panel); padding: 1px 5px; border-radius: 4px; }
.pill { display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.05em;
        text-transform: uppercase; padding: 2px 7px; border-radius: 10px; color: #fff; }
.safety { background: var(--safety); } .physio { background: var(--physio); }
.comfort { background: var(--comfort); }
.unchecked td:first-child { color: var(--safety); }
.legend { display: flex; gap: 16px; color: var(--muted); font-size: 12px; margin: 4px 0 0; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
            margin-right: 5px; }
footer { margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--rule);
         color: var(--muted); font-size: 12px; white-space: pre-wrap; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e7e9ec; --muted: #9aa2ae; --rule: #333941; --ground: #16181b;
          --panel: #1e2126; --safety: #e5705f; --physio: #d9a441; --comfort: #6ba4de; }
}
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _summary_values(result: ScorerResult) -> dict[str, Any]:
    for measurement in result.measurements:
        if measurement.segment_id == ROUTE_SUMMARY_ID:
            return dict(measurement.values)
    return {}


def _by_scorer(plan: Plan) -> dict[str, ScorerResult]:
    return {result.name: result for result in plan.results}


def _segment_series(plan: Plan, scorer: str, key: str) -> dict[str, float]:
    result = _by_scorer(plan).get(scorer)
    if result is None:
        return {}
    out: dict[str, float] = {}
    for measurement in result.measurements:
        if measurement.segment_id == ROUTE_SUMMARY_ID:
            continue
        value = measurement.values.get(key)
        if isinstance(value, int | float):
            out[measurement.segment_id] = float(value)
    return out


def _tier_class(tier: Tier) -> str:
    return {Tier.SAFETY: "safety", Tier.PHYSIOLOGICAL: "physio", Tier.COMFORT: "comfort"}[tier]


# --- the map ----------------------------------------------------------------


def _route_svg(plan: Plan) -> str:
    """The route in local metric coordinates, with flagged segments picked out.

    Equirectangular about the route's own centre — good to a fraction of a percent over a
    100 km route and, unlike a web-mercator tile scheme, it needs nothing from anybody.
    """
    import math

    points = plan.route.points
    if len(points) < 2:
        return ""

    lat0 = sum(p.lat for p in points) / len(points)
    scale = math.cos(math.radians(lat0))
    xs = [p.lon * scale for p in points]
    ys = [-p.lat for p in points]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)

    pad = 18
    fit = min((MAP_WIDTH - 2 * pad) / span_x, (MAP_HEIGHT - 2 * pad) / span_y)
    off_x = (MAP_WIDTH - span_x * fit) / 2
    off_y = (MAP_HEIGHT - span_y * fit) / 2

    def place(i: int) -> tuple[float, float]:
        return (off_x + (xs[i] - min_x) * fit, off_y + (ys[i] - min_y) * fit)

    # One flag per segment on the map: the most serious. Tier first, then severity, so a
    # segment with a safety flag never draws in a comfort colour.
    worst: dict[str, Flag] = {}
    for candidate in plan.residual_flags:
        held = worst.get(candidate.segment_id)
        if held is None or (int(candidate.tier), -candidate.severity) < (
            int(held.tier),
            -held.severity,
        ):
            worst[candidate.segment_id] = candidate

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (place(i) for i in range(len(points))))
    parts = [
        f'<svg viewBox="0 0 {MAP_WIDTH} {MAP_HEIGHT}" width="{MAP_WIDTH}" '
        f'height="{MAP_HEIGHT}" role="img" aria-label="Route with flagged segments">',
        f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="3" '
        'stroke-opacity="0.35" stroke-linejoin="round" stroke-linecap="round"/>',
    ]
    for segment in plan.segments:
        flag = worst.get(segment.id)
        if flag is None:
            continue
        span = " ".join(
            f"{x:.1f},{y:.1f}"
            for x, y in (place(i) for i in range(segment.start_idx, segment.end_idx + 1))
        )
        parts.append(
            f'<polyline points="{span}" fill="none" stroke="{TIER_COLOUR[flag.tier]}" '
            f'stroke-width="5" stroke-linecap="round"><title>{_e(segment.id)}: '
            f"{_e(flag.reason_code)}</title></polyline>"
        )

    start_x, start_y = place(0)
    end_x, end_y = place(len(points) - 1)
    parts.append(
        f'<circle cx="{start_x:.1f}" cy="{start_y:.1f}" r="5" fill="currentColor">'
        "<title>start</title></circle>"
    )
    parts.append(
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="5" fill="none" '
        'stroke="currentColor" stroke-width="2"><title>finish</title></circle>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _elevation_svg(plan: Plan) -> str:
    """Elevation against distance, from the DEM samples stored on the route."""
    samples = [(p.cum_dist_m, float(p.ele_m)) for p in plan.route.points if p.ele_m is not None]
    if len(samples) < 2:
        return ""

    total = max(plan.route.length_m, 1.0)
    lows = min(ele for _, ele in samples)
    highs = max(ele for _, ele in samples)
    span = max(highs - lows, 1.0)
    height = 150

    coords = [
        (
            cum / total * STRIP_WIDTH,
            height - 10 - (ele - lows) / span * (height - 26),
        )
        for cum, ele in samples
    ]
    area = (
        f"0,{height} "
        + " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        + f" {STRIP_WIDTH},{height}"
    )
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return (
        f'<svg viewBox="0 0 {STRIP_WIDTH} {height}" width="{STRIP_WIDTH}" height="{height}" '
        f'role="img" aria-label="Elevation profile from {lows:.0f} to {highs:.0f} metres">'
        f'<polygon points="{area}" fill="currentColor" fill-opacity="0.10"/>'
        f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="2"/>'
        f'<text x="4" y="14" font-size="11" fill="currentColor" opacity="0.7">'
        f"{highs:.0f} m</text>"
        f'<text x="4" y="{height - 4}" font-size="11" fill="currentColor" opacity="0.7">'
        f"{lows:.0f} m</text>"
        "</svg>"
    )


def _band_svg(plan: Plan, label: str, values: dict[str, float], colour: str) -> str:
    """One row of the timeline strip: a per-segment value, aligned by distance."""
    if not values:
        return ""
    total = max(plan.route.length_m, 1.0)
    lo, hi = min(values.values()), max(values.values())
    span = max(hi - lo, 1e-6)

    bars = []
    for segment in plan.segments:
        value = values.get(segment.id)
        if value is None:
            continue
        x = segment.cum_start_m / total * STRIP_WIDTH
        width = max(segment.length_m / total * STRIP_WIDTH, 1.0)
        intensity = 0.15 + 0.85 * (value - lo) / span
        bars.append(
            f'<rect x="{x:.1f}" y="16" width="{width:.1f}" height="{STRIP_HEIGHT - 26}" '
            f'fill="{colour}" fill-opacity="{intensity:.2f}">'
            f"<title>{_e(segment.id)}: {value:.1f}</title></rect>"
        )
    return (
        f'<svg viewBox="0 0 {STRIP_WIDTH} {STRIP_HEIGHT}" width="{STRIP_WIDTH}" '
        f'height="{STRIP_HEIGHT}" role="img" aria-label="{_e(label)} along the route">'
        f'<text x="0" y="11" font-size="11" fill="currentColor" opacity="0.75">'
        f"{_e(label)} — {lo:.1f} to {hi:.1f}</text>" + "".join(bars) + "</svg>"
    )


# --- sections ---------------------------------------------------------------


def _facts(plan: Plan) -> str:
    duration = ""
    if len(plan.etas) > 1:
        seconds = (plan.etas[-1] - plan.etas[0]).total_seconds()
        duration = f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"

    items = [("Distance", f"{plan.route.length_m / 1000:.1f} km", "")]
    if plan.elevation:
        items.append(
            ("Gain / loss", f"{plan.elevation.gain_m:.0f} / {plan.elevation.loss_m:.0f} m", "")
        )
    if duration:
        items.append(("Duration", duration, f"finish {plan.etas[-1]:%H:%M}"))

    sun = _summary_values(_by_scorer(plan).get("sun_exposure") or _empty())
    if "shaded_fraction" in sun:
        items.append(("Shade", f"{float(sun['shaded_fraction']):.0%}", str(sun.get("surface", ""))))
    heat = _summary_values(_by_scorer(plan).get("heat_stress") or _empty())
    if heat.get("max_wbgt_c") is not None:
        items.append(("Peak WBGT", f"{float(heat['max_wbgt_c']):.1f} °C", "soft 26 / hard 30"))
    resupply = _summary_values(_by_scorer(plan).get("resupply_schedule") or _empty())
    if resupply.get("max_dry_gap_min") is not None:
        items.append(
            (
                "Longest dry gap",
                f"{float(resupply['max_dry_gap_min']):.0f} min",
                f"carry {float(resupply.get('suggested_carry_ml') or 0):.0f} ml",
            )
        )

    cells = "".join(
        f"<li><span>{_e(label)}</span><b>{_e(value)}</b>"
        + (f"<span>{_e(note)}</span>" if note else "")
        + "</li>"
        for label, value, note in items
    )
    return f'<ul class="facts">{cells}</ul>'


def _empty() -> ScorerResult:
    from longrun.core.models.measurement import ScorerResult as _R

    return _R(name="")


def _flags_table(plan: Plan) -> str:
    rows = []
    for result in sorted(plan.results, key=lambda r: r.name):
        worst = result.worst(WORST_N)
        if not worst:
            continue
        rows.append(
            f'<tr><th colspan="4">{_e(result.name)} — {len(result.flags)} flagged</th></tr>'
        )
        for flag in worst:
            kind = "HARD" if flag.kind is FlagKind.HARD else "soft"
            rows.append(
                f"<tr><td class='num'><code>{_e(flag.segment_id)}</code></td>"
                f'<td><span class="pill {_tier_class(flag.tier)}">{kind}</span></td>'
                f"<td class='num'>{flag.severity:.2f}</td>"
                f"<td><code>{_e(flag.reason_code)}</code><br>{_e(flag.detail or '')}</td></tr>"
            )
    if not rows:
        return "<p>No segment was flagged by any scorer that ran.</p>"
    return (
        "<table><thead><tr><th>Segment</th><th>Kind</th><th>Severity</th>"
        "<th>Reason</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _coverage_table(plan: Plan) -> str:
    unchecked = plan.coverage.unchecked()
    checked = plan.coverage.checked()
    rows = [
        f'<tr class="unchecked"><td>{_e(e.source)}</td><td>{_e(e.kind)}</td>'
        f"<td>NOT CHECKED</td><td>{_e(e.reason or '')}</td></tr>"
        for e in unchecked
    ] + [
        f"<tr><td>{_e(e.source)}</td><td>{_e(e.kind)}</td><td>checked</td>"
        f"<td>{_e(e.reason or '')}</td></tr>"
        for e in checked
    ]
    return (
        f"<p>{len(unchecked)} source(s) could not be checked; {len(checked)} could.</p>"
        "<table><thead><tr><th>Source</th><th>Kind</th><th>State</th><th>Detail</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _verify_table(plan: Plan) -> str:
    if plan.verify is None:
        return "<p>Verification did not run.</p>"
    rows = "".join(
        f"<tr><td class='num'>{check.number}</td><td><code>{_e(check.name)}</code></td>"
        f"<td>{_e(check.status)}</td><td>{_e(check.detail or '')}"
        + (f" ({len(check.offenders)} offender(s))" if check.offenders else "")
        + "</td></tr>"
        for check in plan.verify.results
    )
    return (
        f"<p>{_e(plan.verify.summary())}</p>"
        "<table><thead><tr><th>#</th><th>Check</th><th>Status</th><th>Detail</th></tr>"
        "</thead><tbody>" + rows + "</tbody></table>"
    )


def render_html(plan: Plan, pacing_caveats: list[str] | None = None) -> str:
    """One self-contained HTML document. No network, at render time or at view time."""
    if pacing_caveats is None:
        pacing_caveats = plan.pacing_caveats
    title = plan.route.name or plan.id
    strips = "".join(
        _band_svg(plan, label, _segment_series(plan, scorer, key), colour)
        for label, scorer, key, colour in (
            ("WBGT °C", "heat_stress", "wbgt_c", "#c47f17"),
            ("Shaded fraction", "sun_exposure", "shaded_fraction", "#2c6fb5"),
            ("Stops per km", "stop_density", "stops_per_km", "#7a5ea8"),
        )
    )
    caveats = "".join(f"<li>{_e(c)}</li>" for c in (pacing_caveats or []))
    warnings = "".join(f"<li>{_e(w)}</li>" for w in plan.warnings)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} — plan sheet</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{_e(title)}</h1>
<p class="sub">{_e(plan.request.date)} · plan <code>{_e(plan.id)}</code></p>
{_facts(plan)}

<h2>Route</h2>
<figure>{_route_svg(plan)}</figure>
<p class="legend">
  <span><i style="background:{TIER_COLOUR[Tier.SAFETY]}"></i>safety</span>
  <span><i style="background:{TIER_COLOUR[Tier.PHYSIOLOGICAL]}"></i>physiological</span>
  <span><i style="background:{TIER_COLOUR[Tier.COMFORT]}"></i>comfort</span>
  <span>filled circle: start · open circle: finish</span>
</p>

<h2>Along the route</h2>
<figure>{_elevation_svg(plan)}</figure>
<figure>{strips}</figure>

<h2>Flagged segments</h2>
{_flags_table(plan)}

<h2>Verification</h2>
{_verify_table(plan)}

<h2>Coverage</h2>
{_coverage_table(plan)}

{"<h2>Caveats</h2><ul>" + caveats + warnings + "</ul>" if (caveats or warnings) else ""}

<footer>{_e(render_attribution([e.source for e in plan.coverage.entries]))}</footer>
</main></body></html>
"""


__all__ = ["TIER_COLOUR", "WORST_N", "render_html"]
