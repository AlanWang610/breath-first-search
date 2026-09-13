"""The plan sheet as markdown (scope 9).

The sheet's job is not to say a route is good. It is to say what was measured, what was
flagged, what was tried, and — this is the part that is easy to drop and hardest to add
back — **what was not checked at all**. A sheet that omits the coverage manifest reads as
more authoritative than the plan actually is.

Rendering order follows scope 9. Sections with nothing to say are still printed, saying
so, rather than silently vanishing: an absent "water and toilets" heading is
indistinguishable from a route with no water problems.
"""

from __future__ import annotations

from datetime import datetime

from longrun.core.export import attribution
from longrun.core.geo.dem import ElevationProfile
from longrun.core.models.measurement import Flag, FlagKind, Tier
from longrun.core.models.plan import Plan
from longrun.core.models.verification import VerifyReport

WORST_N = 5


def _fmt_duration(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h {rest // 60:02d}m"


def _fmt_time(when: datetime | None) -> str:
    return when.strftime("%H:%M") if when else "unknown"


def _flag_line(flag: Flag) -> str:
    mark = "HARD" if flag.kind is FlagKind.HARD else "soft"
    detail = f" — {flag.detail}" if flag.detail else ""
    return (
        f"  - `{flag.segment_id}` [{mark}/{Tier(flag.tier).name.lower()}] "
        f"`{flag.reason_code}` severity {flag.severity:.2f}{detail}"
    )


def render_markdown(
    plan: Plan,
    elevation: ElevationProfile | None = None,
    verify: VerifyReport | None = None,
    pacing_caveats: list[str] | None = None,
) -> str:
    """Render a plan as a markdown sheet.

    `pacing_caveats` defaults to the plan's own. Passing them explicitly is the older
    path and still wins, so a caller holding a fresher `ETAVector` than the stored plan
    can say so.
    """
    if pacing_caveats is None:
        pacing_caveats = plan.pacing_caveats
    out: list[str] = []
    route = plan.route

    out.append(f"# Plan {plan.id}")
    out.append("")
    out.append(f"{route.name or route.id} — {plan.request.date.isoformat()}")
    out.append("")

    out.append("## Summary")
    out.append("")
    out.append(f"- Distance: {route.length_m / 1000:.1f} km")
    if elevation and elevation.has_elevation:
        out.append(f"- Gain / loss: {elevation.gain_m:.0f} m / {elevation.loss_m:.0f} m")
    else:
        out.append("- Gain / loss: not computed (no elevation data)")
    if plan.etas:
        total = (plan.etas[-1] - plan.etas[0]).total_seconds()
        out.append(
            f"- Projected duration: {_fmt_duration(total)} "
            f"(start {_fmt_time(plan.etas[0])}, finish {_fmt_time(plan.finish_time)})"
        )
    else:
        out.append("- Projected duration: not computed")
    out.append("")

    out.extend(_metrics_section(plan))
    out.extend(_elevation_section(elevation))
    out.extend(_flags_section(plan))
    out.extend(_trade_offs_section(plan))
    out.extend(_verify_section(verify))
    out.extend(_warnings_section(plan, pacing_caveats))
    out.extend(_coverage_section(plan))
    out.extend(_manifest_section(plan))
    out.extend(_attribution_section(plan))

    return "\n".join(out).rstrip() + "\n"


def _metrics_section(plan: Plan) -> list[str]:
    """Scope 7.1's acceptance metrics, when there are any.

    Omitted entirely rather than printed as three "unknown"s on a plan nobody asked for
    them on - `longrun metrics` is what fills this, and a sheet that always carried an
    empty block would train a reader to skip it.

    The detour ratio says "not measured" where it was not, never 1.0. That distinction is
    the whole reason `AcceptanceMetrics` uses `None`: risk R1 turns on whether these
    numbers move, and a fabricated 1.0 would read as "this route is already shortest".
    """
    if not plan.metrics:
        return []
    out = ["## Acceptance metrics", ""]
    fraction = plan.metrics.get("fraction_lts3_plus")
    count = plan.metrics.get("lts4_count")
    ratio = plan.metrics.get("detour_ratio")
    shortest = plan.metrics.get("shortest_legal_m")
    out.append(
        "- Length at LTS 3 or worse: " + ("not measured" if fraction is None else f"{fraction:.1%}")
    )
    out.append("- LTS 4 segments: " + ("not measured" if count is None else str(count)))
    if ratio is None or shortest is None:
        out.append("- Detour vs shortest legal route: not measured")
    else:
        out.append(
            f"- Detour vs shortest legal route: {float(ratio):.3f}x "
            f"({float(shortest) / 1000:.2f} km shortest legal)"
        )
    for reason in plan.metrics.get("reasons") or []:
        out.append(f"- {reason}")
    out.append("")
    return out


def _elevation_section(elevation: ElevationProfile | None) -> list[str]:
    out = ["## Elevation", ""]
    if elevation is None or not elevation.has_elevation:
        out += ["No elevation data was sampled for this route.", ""]
        return out
    out.append(f"- Range: {elevation.min_ele_m:.0f}–{elevation.max_ele_m:.0f} m")
    if elevation.longest_climb:
        climb = elevation.longest_climb
        out.append(
            f"- Longest climb: {climb.length_m / 1000:.1f} km at "
            f"{climb.mean_grade_pct:.1f}% from {climb.start_m / 1000:.1f} km"
        )
    if elevation.longest_descent:
        drop = elevation.longest_descent
        out.append(
            f"- Longest descent: {drop.length_m / 1000:.1f} km at "
            f"{drop.mean_grade_pct:.1f}% from {drop.start_m / 1000:.1f} km"
        )
    if elevation.samples_missing:
        out.append(f"- Elevation missing at {elevation.samples_missing} points")
    out.append("")
    return out


def _flags_section(plan: Plan) -> list[str]:
    """Worst N per scorer with reasons (scope 3.4: no single aggregate score)."""
    out = ["## Flagged segments", ""]
    if not plan.results:
        out += ["No scorers were run.", ""]
        return out

    any_flags = False
    for result in sorted(plan.results, key=lambda r: r.name):
        worst = result.worst(WORST_N)
        if not worst:
            continue
        any_flags = True
        out.append(f"- **{result.name}** ({len(result.flags)} flagged)")
        out.extend(_flag_line(flag) for flag in worst)
    if not any_flags:
        out.append("No segment was flagged by any scorer that ran.")
    out.append("")
    return out


def _trade_offs_section(plan: Plan) -> list[str]:
    """Same-tier choices left to the user (scope 8.4)."""
    if not plan.trade_offs:
        return []
    out = ["## Choices left to you", ""]
    for trade in plan.trade_offs:
        out.append(f"- `{trade.segment_id}`: {trade.option_a} vs {trade.option_b}")
        out.append(f"  {trade.comparison}")
    out.append("")
    return out


def _verify_section(verify: VerifyReport | None) -> list[str]:
    if verify is None:
        return []
    out = ["## Verification", "", f"{verify.summary()}.", ""]
    for check in verify.results:
        if check.status == "passed":
            continue
        label = "FAILED" if check.status == "failed" else "skipped"
        detail = f" — {check.detail}" if check.detail else ""
        out.append(f"- [{label}] {check.number}. {check.name}{detail}")
        out.extend(f"  - {offender}" for offender in check.offenders[:WORST_N])
    out.append("")
    return out


def _warnings_section(plan: Plan, pacing_caveats: list[str] | None) -> list[str]:
    """Explicit warnings (scope 9): pacing extrapolation, missing data."""
    warnings = [*(pacing_caveats or []), *plan.warnings]
    if not warnings:
        return []
    out = ["## Warnings", ""]
    out.extend(f"- {w}" for w in warnings)
    out.append("")
    return out


def _coverage_section(plan: Plan) -> list[str]:
    """Which sources were checked and which were not (scope 3.6, 9).

    The unchecked list comes first and is never omitted. It is the part of the sheet that
    keeps the rest honest.
    """
    out = ["## Coverage", ""]
    unchecked = plan.coverage.unchecked()
    checked = plan.coverage.checked()

    if unchecked:
        out.append(f"**Not checked ({len(unchecked)}):**")
        out.append("")
        out.extend(f"- {entry}" for entry in unchecked)
        out.append("")
    else:
        out += ["Every source consulted for this plan returned data.", ""]

    if checked:
        out.append(f"**Checked ({len(checked)}):**")
        out.append("")
        out.extend(f"- {entry}" for entry in checked)
        out.append("")
    return out


def _manifest_section(plan: Plan) -> list[str]:
    """Data-snapshot pins and budgets used (scope 6.4, 9)."""
    out = ["## Manifest", ""]
    snapshot = plan.manifest.snapshot
    pins = {
        "OSM extract": snapshot.osm_extract_date,
        "HPMS vintage": snapshot.hpms_vintage,
        "DEM resolution (m)": snapshot.dem_resolution_m,
        "Canopy version": snapshot.canopy_version,
    }
    recorded = {k: v for k, v in pins.items() if v is not None}
    if recorded:
        out.extend(f"- {k}: {v}" for k, v in recorded.items())
    else:
        out.append("- No data-snapshot versions recorded.")

    if plan.manifest.tool_calls:
        out.append(
            f"- {len(plan.manifest.tool_calls)} tool calls, "
            f"{plan.manifest.total_elapsed_s:.1f} s total"
        )
    # Always, including the zero. Scope 6.4 puts budgets in the manifest and scope 9 puts
    # the manifest on the sheet, and "this plan replayed a cassette and called nothing" is
    # the more interesting of the two readings - a number that only appears when it is
    # non-zero cannot report that.
    out.append(
        f"- {plan.manifest.api_calls_used} external API call(s), "
        f"{plan.manifest.imagery_tiles_used} imagery tile(s)"
    )
    if plan.manifest.degradation:
        out.append(f"- Degraded: {', '.join(plan.manifest.degradation)}")
    out.append("")
    return out


def _attribution_section(plan: Plan) -> list[str]:
    sources = [entry.source for entry in plan.coverage.checked()]
    return ["## Attribution", "", attribution.render(sources), ""]
