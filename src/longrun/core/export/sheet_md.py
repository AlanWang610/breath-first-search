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
from typing import Any

from longrun.core.export import attribution
from longrun.core.geo.dem import ElevationProfile
from longrun.core.models.measurement import Flag, FlagKind, Tier
from longrun.core.models.plan import Plan
from longrun.core.models.verification import VerifyReport

WORST_N = 5

#: Meet points shown in the crew table before it is thinned by spacing. One golden route
#: reports 201 of them, and a table of all 201 is a table nobody reads.
MEET_ROWS = 12


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
    out.extend(_resupply_section(plan))
    out.extend(_sun_heat_section(plan))
    out.extend(_start_time_section(plan))
    out.extend(_flags_section(plan))
    out.extend(_trade_offs_section(plan))
    out.extend(_logistics_section(plan))
    out.extend(_cue_section(plan))
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


def _cue_section(plan: Plan) -> list[str]:
    """Scope 9's cue sheet - the artifact a runner actually carries.

    Always printed, per this module's rule. Every cue rather than a worst-N: a truncated
    cue sheet is useless, and 400 turns is ~24 kB against the 400 kB the sheet is capped at.

    The four "not produced" reasons are four different facts and are never collapsed into
    "no turns": a runner told a route has none, when it has forty, has been misinformed.
    """
    out = ["## Cue sheet", ""]
    sheet = plan.cues
    if not sheet.checked:
        out.append(f"Not produced: {sheet.reason or 'no reason recorded'}.")
        out.append("")
        return out
    if not sheet.cues:
        out.append("The router returned no turns for this route.")
        out.append("")
        return out

    ambiguous = len(sheet.ambiguous)
    summary = f"{sheet.turn_count} turn(s) over {plan.route.length_m / 1000:.1f} km."
    if ambiguous:
        summary += f" {ambiguous} flagged as ambiguous."
    if sheet.frame_shift_m:
        summary += (
            f" Drawn on a line {sheet.frame_shift_m:.0f} m different in length from the one "
            f"this plan holds, because the route was matched to the graph after routing."
        )
    out += [summary, ""]
    for cue in sheet.cues:
        street = cue.street_name or "unnamed way"
        line = f"- {cue.cum_dist_m / 1000:6.2f} km  {cue.manoeuvre:<12} {street}"
        if cue.ambiguous and cue.ambiguity:
            line += f"  — {cue.ambiguity}"
        out.append(line)
    out.append("")
    return out


def _summary_of(plan: Plan, scorer: str) -> dict[str, Any] | None:
    """One scorer's route-level measurement, or None if it did not run.

    Every section below is a *rendering* of numbers that have existed since the scorer that
    produces them landed. None of them measures anything new.
    """
    from longrun.core.scorers._common import ROUTE_SUMMARY_ID

    result = plan.result(scorer)
    if result is None:
        return None
    for measurement in result.measurements:
        if measurement.segment_id == ROUTE_SUMMARY_ID:
            return dict(measurement.values)
    return None


def _not_run(plan: Plan, scorer: str) -> str:
    """Why a scorer produced nothing, read from the coverage manifest rather than guessed."""
    for entry in plan.coverage.entries:
        if entry.source == scorer and not entry.checked:
            return entry.reason or "not checked"
    return "did not run"


def _resupply_section(plan: Plan) -> list[str]:
    """Scope 9: water and toilet gaps **in minutes**, and a suggested carry.

    Minutes rather than metres is why `resupply_schedule` exists beside `services_along`: a
    fountain 400 m ahead and a cafe 400 m ahead are the same distance and not the same fact
    at 06:30.
    """
    out = ["## Water, toilets and carry", ""]
    values = _summary_of(plan, "resupply_schedule")
    if values is None:
        out += [f"Not established: {_not_run(plan, 'resupply_schedule')}.", ""]
        return out

    def _minutes(key: str) -> str:
        value = values.get(key)
        return "none found" if not isinstance(value, int | float) else f"{float(value):.0f} min"

    out += [
        f"- Longest stretch with no **open water**: {_minutes('max_dry_gap_min')}",
        f"- Longest with no **open toilet**: {_minutes('max_toilet_gap_min')}",
        f"- Longest with no **open food**: {_minutes('max_food_gap_min')}",
    ]
    carry = values.get("suggested_carry_ml")
    capacity = values.get("carry_capacity_ml")
    if isinstance(carry, int | float):
        line = f"- Suggested carry: **{float(carry):.0f} ml**"
        if isinstance(capacity, int | float):
            line += f" against a {float(capacity):.0f} ml capacity"
            if float(carry) > float(capacity):
                line += " - you will need to refill"
        out.append(line)
    wbgt = values.get("wbgt_used_c")
    if isinstance(wbgt, int | float):
        out.append(
            f"- Thresholds scaled for WBGT {float(wbgt):.1f} C "
            f"(scope 8.3: the dry-gap limits shrink as it rises)"
        )
    unreadable = values.get("services_with_unreadable_hours")
    if isinstance(unreadable, int | float) and unreadable:
        out.append(
            f"- {int(unreadable)} place(s) had opening hours this build cannot parse, and are "
            f"counted as available with reduced confidence"
        )
    out.append("")
    return out


def _sun_heat_section(plan: Plan) -> list[str]:
    """Scope 9: sun and heat **by hour**, and daylight status.

    There is no hourly series anywhere in a plan, so this buckets the *per-segment*
    measurements by the clock hour of their ETA. **The hours come from the ETA vector and
    the values from segments** - worth saying, because a reader would otherwise assume a
    forecast timeseries.
    """
    from longrun.core.scorers._common import ROUTE_SUMMARY_ID

    out = ["## Sun and heat by hour", ""]
    heat = plan.result("heat_stress")
    sun = plan.result("sun_exposure")
    if heat is None and sun is None:
        out += ["Not established: neither heat stress nor sun exposure ran.", ""]
        return out

    by_segment = {s.id: s for s in plan.segments}
    buckets: dict[int, dict[str, list[float]]] = {}
    for result, keys in ((heat, ("wbgt_c",)), (sun, ("shaded_fraction",))):
        if result is None:
            continue
        for measurement in result.measurements:
            if measurement.segment_id == ROUTE_SUMMARY_ID:
                continue
            segment = by_segment.get(measurement.segment_id)
            if segment is None or not plan.etas:
                continue
            hour = plan.etas[min(segment.start_idx, len(plan.etas) - 1)].hour
            slot = buckets.setdefault(hour, {})
            slot.setdefault("km", []).append(segment.cum_start_m / 1000.0)
            for key in keys:
                value = measurement.values.get(key)
                if isinstance(value, int | float):
                    slot.setdefault(key, []).append(float(value))

    if buckets:
        out += ["| Hour | From km | Max WBGT | Mean shade |", "|---|---|---|---|"]
        for hour in sorted(buckets):
            slot = buckets[hour]
            wbgt = max(slot.get("wbgt_c", []), default=None)
            shade = slot.get("shaded_fraction", [])
            out.append(
                f"| {hour:02d}:00 | {min(slot.get('km', [0.0])):.1f} | "
                f"{'-' if wbgt is None else f'{wbgt:.1f} C'} | "
                f"{'-' if not shade else f'{sum(shade) / len(shade) * 100:.0f}%'} |"
            )
        out.append("")

    lighting = _summary_of(plan, "lighting")
    if lighting is not None:
        starts = lighting.get("starts_in_daylight")
        finishes = lighting.get("finishes_in_daylight")
        out.append(
            f"Daylight: starts {'in daylight' if starts else 'after dark'}, "
            f"finishes {'in daylight' if finishes else 'after dark'}."
        )
        dark_m = lighting.get("dark_m")
        if isinstance(dark_m, int | float) and dark_m:
            unlit = lighting.get("unlit_dark_m")
            unlit_km = float(unlit) / 1000 if isinstance(unlit, int | float) else 0.0
            out.append(
                f"{float(dark_m) / 1000:.2f} km is run in darkness, of which {unlit_km:.2f} km "
                f"is on ways tagged unlit."
            )
        out.append("")
    return out


def _start_time_section(plan: Plan) -> list[str]:
    """Scope 9's start-time table.

    Gated on the **scorer's candidates**, not on `PlanRequest.start_window`. That field has
    two references in the whole tree - its own declaration and its own validator - so no CLI
    sets it and `start_time_optimizer` ignores it entirely. A section gated on it would
    render on zero plans, including all six goldens.

    So the window here is the sweep the optimizer ran, **not** a constraint the user gave.
    Those are different claims and the sheet must not conflate them.
    """
    from longrun.core.scorers.start_time_optimizer import CANDIDATE_PREFIX

    result = plan.result("start_time_optimizer")
    if result is None:
        return []
    rows = [m for m in result.measurements if m.segment_id.startswith(CANDIDATE_PREFIX)]
    if not rows:
        return []

    out = [
        "## If you started at a different time",
        "",
        "The optimizer's own sweep around the requested start, not a window you asked for.",
        "",
        "| Start | Finish | Sunlit | Dark min | Peak WBGT |",
        "|---|---|---|---|---|",
    ]
    requested = plan.etas[0].strftime("%H:%M") if plan.etas else None
    for row in rows:
        values = row.values

        def _num(key: str, digits: str, scale: float = 1.0, values: Any = values) -> str:
            value = values.get(key)
            if not isinstance(value, int | float):
                return "-"
            return format(float(value) * scale, digits)

        start = str(values.get("start") or row.segment_id[len(CANDIDATE_PREFIX) :])
        mark = " <-" if requested and start == requested else ""
        out.append(
            f"| {start}{mark} | {values.get('finish') or '-'} | "
            f"{_num('sunlit_fraction', '.0f', 100.0)}% | {_num('dark_minutes', '.0f')} | "
            f"{_num('peak_wbgt_c', '.1f')} |"
        )

    summary = _summary_of(plan, "start_time_optimizer") or {}
    hints = []
    if summary.get("coolest_start"):
        hints.append(f"coolest {summary['coolest_start']}")
    if summary.get("shadiest_start"):
        hints.append(f"shadiest {summary['shadiest_start']}")
    if hints:
        out += ["", "Best on this route: " + ", ".join(hints) + "."]
    out.append("")
    return out


def _logistics_section(plan: Plan) -> list[str]:
    """Scope 9: bailouts and crew points, with times.

    Bailouts render **without** a time in their measurement, and none is added to it: the
    renderer already has `plan.etas` and `plan.segments`, and putting an ETA into
    `SegmentMeasurement.values` would move `measurements_sha256` on six routes for a value
    that is derivable right here.
    """
    from longrun.core.scorers.crew_points import MEET_PREFIX

    out = ["## Getting out, and being met", ""]

    result = plan.result("bailouts")
    if result is None:
        out.append(f"Bailouts: not established - {_not_run(plan, 'bailouts')}.")
    else:
        stranded = [f for f in result.flags if f.reason_code == "no_bailout_in_reach"]
        if not stranded:
            out.append("Every segment has an exit within reach.")
        else:
            by_segment = {s.id: s for s in plan.segments}
            out.append(f"**{len(stranded)} segment(s) with no exit in reach:**")
            for flag in stranded[:WORST_N]:
                segment = by_segment.get(flag.segment_id)
                when = ""
                if segment is not None and plan.etas:
                    when = f" (~{plan.etas[min(segment.start_idx, len(plan.etas) - 1)]:%H:%M})"
                where = "" if segment is None else f"{segment.cum_start_m / 1000:.1f} km"
                out.append(f"- {where}{when} - {flag.detail or flag.reason_code}")
            if len(stranded) > WORST_N:
                out.append(f"- ...and {len(stranded) - WORST_N} more, in `plan.json`")
    out.append("")

    crew = plan.result("crew_points")
    if crew is None:
        out += [f"Crew access: not established - {_not_run(plan, 'crew_points')}.", ""]
        return out

    meets = [m for m in crew.measurements if m.segment_id.startswith(MEET_PREFIX)]
    if not meets:
        out += ["No road-accessible parking was found near this route.", ""]
        return out

    # Thinned rather than truncated, for the same reason the device cap is: one golden route
    # reports 201 meet points, and a table of all 201 is a table nobody reads.
    shown = meets
    if len(meets) > MEET_ROWS:
        spacing = plan.route.length_m / MEET_ROWS if plan.route.length_m else 0.0
        shown, last = [], -spacing - 1.0
        for meet in meets:
            at = meet.values.get("cum_dist_m")
            if isinstance(at, int | float) and float(at) - last >= spacing:
                shown.append(meet)
                last = float(at)

    out += [
        f"**{len(meets)} meet point(s)**"
        + (f", {len(shown)} shown:" if len(shown) < len(meets) else ":"),
        "",
        "| km | Runner ETA | Off route | To a road | Name |",
        "|---|---|---|---|---|",
    ]
    for meet in shown:
        values = meet.values

        def _metres(key: str, values: Any = values) -> str:
            value = values.get(key)
            return "-" if not isinstance(value, int | float) else f"{float(value):.0f} m"

        at = values.get("cum_dist_m")
        km = f"{float(at) / 1000:.1f}" if isinstance(at, int | float) else "-"
        out.append(
            f"| {km} | {values.get('runner_eta') or '-'} | {_metres('offset_m')} | "
            f"{_metres('road_m')} | {values.get('name') or '-'} |"
        )
    if len(shown) < len(meets):
        out += ["", f"The other {len(meets) - len(shown)} are in `plan.json`."]
    out.append("")
    return out
