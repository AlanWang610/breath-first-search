"""What a verdict's bounds are checked against, read the same way from a plan and a digest.

Moved out of `tests/eval/test_eval_routes.py` in M23.1 because a second reader arrived: the
label door has to refuse a verdict the eval suite would refuse, at the moment a person
writes it rather than on the next CI run. Two copies of one rule drift, and the copy that
drifts is the one nobody runs.

**A bound is always the acceptable envelope** (ADR 0022), never a description of what is
wrong with a route. A verdict of `no` is one whose measurements fall *outside* its bounds;
stating an unrunnable route's bounds the other way round - "this route has at least 15% at
LTS 3" - reads naturally and makes the breach check pass without checking anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.plan import Plan

#: Every metric a bound may name, and what it means in words a label form can show.
#:
#: The first four were the whole list until M23: scope 7.1's three acceptance metrics, and
#: the hard-flag count phoenix-heat's `no` rests on. The rest are what a runner's `no` is
#: usually about and what every golden digest already carries - distance, climbing, surface,
#: water, a way out, and running in the dark. A reason that names none of them is recorded
#: with `gap:` rather than refused (ADR 0022, amended).
METRICS: dict[str, str] = {
    "fraction_lts3_plus": "share of the scored length at LTS 3 or worse",
    "lts4_count": "number of LTS 4 segments",
    "hard_flags": "hard flags from every scorer, before arbitration",
    "detour_ratio": "length over the shortest legal route",
    "scored_fraction": "share of the route matched to a way and scored for traffic stress",
    "length_m": "route length, in metres",
    "gain_m": "elevation gain, in metres",
    "unpaved_fraction": "share of the length on an unpaved surface",
    "max_dry_gap_min": "longest stretch without water, in minutes",
    "max_water_gap_m": "longest stretch without water, in metres",
    "worst_exit_m": "farthest the route gets from a way out, in metres",
    "unlit_dark_m": "metres run in the dark on unlit ways",
}

#: Metrics that are one value in one scorer's route summary, and where. Read by name rather
#: than by position for the reason `plan.metrics._hostility_summary` gives.
SUMMARY_READS: dict[str, tuple[str, str]] = {
    "fraction_lts3_plus": ("segment_hostility", "fraction_lts3_plus"),
    "lts4_count": ("segment_hostility", "lts4_count"),
    "unpaved_fraction": ("surface_profile", "unpaved_fraction"),
    "max_dry_gap_min": ("resupply_schedule", "max_dry_gap_min"),
    "max_water_gap_m": ("services_along", "max_water_gap_m"),
    "worst_exit_m": ("bailouts", "worst_exit_m"),
    "unlit_dark_m": ("lighting", "unlit_dark_m"),
}

#: `<metric>_max` or `<metric>_min`. A typo in a bound key would otherwise be a bound that
#: silently never ran, which is the one failure a suite of assertions cannot survive.
BOUND_NAMES = frozenset(f"{metric}_{side}" for metric in METRICS for side in ("max", "min"))

WOULD_RUN = ("yes", "no", "unsure")

#: A reason shorter than this is a word, not a sentence, and unreviewable a month later.
MIN_REASON_WORDS = 12

#: Digits kept, matching `tests/golden/expectation.py`: scorer summary values at its
#: `SEVERITY_DP`, route length and climbing at its `QUANTITY_DP`. Rounded here so a verdict
#: written against a live plan and re-checked against that plan's golden digest compares
#: the same numbers, rather than a raw float against its own rounding.
_SUMMARY_DP = 3
_QUANTITY_DP = 1

Measured = dict[str, float | int | None]


def _rounded(value: Any, places: int) -> Any:
    return round(value, places) if isinstance(value, float) else value


def _measured(
    summaries: Mapping[str, Mapping[str, Any] | None],
    *,
    hard_flags: int,
    length_m: float | None,
    gain_m: float | None,
    detour_ratio: float | None,
    residual_flags: int | None,
) -> Measured:
    out: Measured = {}
    for metric, (scorer, key) in SUMMARY_READS.items():
        value = (summaries.get(scorer) or {}).get(key)
        out[metric] = None if value is None else _rounded(value, _SUMMARY_DP)

    hostility = summaries.get("segment_hostility") or {}
    scored, total = hostility.get("scored_m"), hostility.get("length_m")
    out["scored_fraction"] = (
        round(scored / total, _SUMMARY_DP) if scored is not None and total else None
    )

    out["hard_flags"] = hard_flags
    out["detour_ratio"] = detour_ratio
    out["length_m"] = None if length_m is None else round(length_m, _QUANTITY_DP)
    out["gain_m"] = None if gain_m is None else round(gain_m, _QUANTITY_DP)
    out["residual_flags"] = residual_flags
    return out


def measured_from_plan(plan: Plan) -> Measured:
    """The numbers a person saw when they judged this plan."""
    from longrun.core.models.measurement import FlagKind
    from longrun.core.scorers._common import ROUTE_SUMMARY_ID

    summaries = {
        result.name: next(
            (dict(m.values) for m in result.measurements if m.segment_id == ROUTE_SUMMARY_ID),
            None,
        )
        for result in plan.results
    }
    return _measured(
        summaries,
        hard_flags=sum(1 for r in plan.results for f in r.flags if f.kind is FlagKind.HARD),
        length_m=plan.route.length_m,
        gain_m=plan.elevation.gain_m if plan.elevation else None,
        detour_ratio=plan.metrics.get("detour_ratio"),
        residual_flags=len(plan.residual_flags),
    )


def measured_from_digest(digest: Mapping[str, Any]) -> Measured:
    """The same numbers, from a golden's `expected.json`.

    `detour_ratio` is always `None` here: the digest does not carry `plan.metrics`, and a
    golden's router is a port nothing listens on, so no golden plan has one to carry. A
    bound on it is refused by `unmeasured` rather than skipped - loop-bayarea's
    `detour_ratio_max: 1.15` sat unchecked from M6 until M23 for exactly this reason.
    """
    scorers: Mapping[str, Any] = digest.get("scorers") or {}
    residual = digest.get("residual_flags")
    return _measured(
        {name: (entry or {}).get("summary") for name, entry in scorers.items()},
        hard_flags=sum(
            ((entry or {}).get("flags") or {}).get("hard", 0) for entry in scorers.values()
        ),
        length_m=(digest.get("route") or {}).get("length_m"),
        gain_m=(digest.get("elevation") or {}).get("gain_m"),
        detour_ratio=None,
        residual_flags=None if residual is None else len(residual),
    )


def _split(bound: str) -> tuple[str, str]:
    metric, _, side = bound.rpartition("_")
    return metric, side


def breaches(bounds: Mapping[str, float], measured: Measured) -> list[str]:
    """Which stated bounds the measurements fall outside, as sentences."""
    out: list[str] = []
    for bound, limit in bounds.items():
        metric, side = _split(bound)
        value = measured.get(metric)
        if value is None:
            continue
        if side == "max" and value > limit:
            out.append(f"{metric} {value} > {limit}")
        if side == "min" and value < limit:
            out.append(f"{metric} {value} < {limit}")
    return out


def unmeasured(bounds: Mapping[str, float], measured: Measured) -> list[str]:
    """Stated bounds on a metric this route has no value for - a bound that cannot run."""
    return [bound for bound in bounds if measured.get(_split(bound)[0]) is None]


def check_verdict(
    *,
    would_run: str,
    reason: str,
    bounds: Mapping[str, Any],
    gap: str | None,
    measured: Measured,
) -> list[str]:
    """Everything wrong with a verdict, in words; an empty list is a verdict that stands.

    The same four properties `tests/eval` asserts, checked at the moment of writing so a
    person hears "your bound says the route is fine" while the route is still on the screen.
    """
    problems: list[str] = []
    if would_run not in WOULD_RUN:
        problems.append(f"would_run must be one of {', '.join(WOULD_RUN)}, not {would_run!r}")
    if len(reason.split()) < MIN_REASON_WORDS:
        problems.append(
            f"the reason is {len(reason.split())} word(s); write at least {MIN_REASON_WORDS} "
            "so it can be reviewed later"
        )

    unknown = sorted(set(bounds) - BOUND_NAMES)
    if unknown:
        problems.append(f"unknown bound(s) {unknown}; a bound is <metric>_max or <metric>_min")
    bad = sorted(
        bound
        for bound, limit in bounds.items()
        if bound in BOUND_NAMES and (isinstance(limit, bool) or not isinstance(limit, int | float))
    )
    if bad:
        problems.append(f"bound(s) {bad} must be numbers")
    if unknown or bad:
        return problems

    cannot_run = unmeasured(bounds, measured)
    if cannot_run:
        problems.append(
            f"this route has no value for {', '.join(_split(b)[0] for b in cannot_run)}, "
            "so a bound on it could never be checked"
        )

    if gap is not None:
        if not gap.strip():
            problems.append("gap: has to say what nothing measures, not be blank")
        elif would_run != "no":
            problems.append("gap: is for a 'no' whose reason no measured number captures")
        elif breaches(bounds, measured):
            problems.append(
                "a stated bound already breaches, so a number does say why; drop gap: "
                f"({'; '.join(breaches(bounds, measured))})"
            )
        return problems

    if not bounds:
        problems.append(
            "state at least one bound - the acceptable envelope - or say with gap: what "
            "made you decide that nothing here measures"
        )
        return problems

    found = breaches(bounds, measured)
    if would_run == "no" and not found:
        problems.append(
            "a 'no' has to breach one of its own bounds; these all hold, so they describe a "
            "route you would run. A bound is the envelope you would accept, not the problem."
        )
    if would_run in ("yes", "unsure") and found:
        problems.append(
            f"labelled {would_run!r} but already outside its bounds: {'; '.join(found)}"
        )
    if would_run == "yes" and measured.get("residual_flags") is None:
        problems.append("labelled 'yes' but the plan lists no residual flags at all")
    return problems


__all__ = [
    "BOUND_NAMES",
    "METRICS",
    "MIN_REASON_WORDS",
    "SUMMARY_READS",
    "WOULD_RUN",
    "Measured",
    "breaches",
    "check_verdict",
    "measured_from_digest",
    "measured_from_plan",
    "unmeasured",
]
