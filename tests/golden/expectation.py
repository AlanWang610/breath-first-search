"""Turning a `Plan` into the thing a golden test actually compares.

A per-segment dump of every scorer is a file nobody reviews and every refactor rewrites,
so `expected.json` is a **summary** — and the summary is chosen so that the parts a human
would check by hand are the parts stored in full.

What is stored, and why each:

* **worst-N per scorer**, as `(segment_id, severity, reason_code)`. Scope 3.4 says the
  worst segments with reasons are a scorer's product, so they are also its expectation.
  Reason *codes*, never prose: `detail` is free to be rewritten without a golden diff.
* **flag counts, split hard from soft.** A soft flag becoming hard is a tier change with
  real consequences under scope 8.4 arbitration, and it would not otherwise show up unless
  the flag also entered the worst-N.
* **mean confidence per scorer.** Scope 12's degradation is invisible in severities: a
  scorer that quietly stops knowing surfaces still reports the same flags at half the
  confidence, and that is exactly the regression worth catching.
* **the coverage manifest, verbatim.** It is the scope 3.6 deliverable. A source silently
  moving from checked to unchecked is the failure mode the manifest exists to prevent, so
  it cannot be summarised into a count.
* **all ten verification results**, with offender counts. Three states, per scope 7.9: a
  check sliding from `skipped` to `passed` without gaining an input is a bug that a
  pass/fail count would hide.
* **each scorer's route-level summary.** Crossings per km, the longest dry stretch,
  stops per km: the headline numbers a reader would check against the map. Leaving them
  inside the hash would mean a route whose signalized crossing stopped being detected as
  a crossing at all produced an identical-looking expectation, because it has no flag
  either way.
* **route and elevation totals.** Gain and loss are headline numbers a reader checks
  against the map, and they are what the pacing figure is derived from — so a change in
  one that does not move the other is a bug worth seeing directly.
* **a content hash of the full measurement array.** Everything above is a summary, and a
  summary can miss things. The hash cannot: any change to any measured value on any
  segment fails the test, and the small diff above is what you read to find out why.

Floats are rounded at serialization and never compared as raw f64 — the same route scored
on Windows and Linux differs in the last bits, and a golden that fails on that is a golden
that gets regenerated instead of read.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.measurement import ScorerResult
    from longrun.core.models.plan import Plan

#: Severities and confidences are 0-1; three places is finer than any threshold in the
#: codebase and coarse enough to survive a different libm.
SEVERITY_DP = 3

#: Metres and seconds. A tenth of either is below anything a reader would act on.
QUANTITY_DP = 1

#: Rounding applied to measured values before hashing. Six places is far below the
#: precision of any physical quantity here and far above cross-platform f64 drift.
HASH_DP = 6

#: How many flags per scorer the expectation carries. Scope 3.4's product is the worst
#: segments with reasons, not all of them.
WORST_N = 5


def _round(value: Any, places: int) -> Any:
    return round(value, places) if isinstance(value, float) else value


def _scorer_digest(result: ScorerResult) -> dict[str, Any]:
    from longrun.core.models.measurement import FlagKind
    from longrun.core.scorers._common import ROUTE_SUMMARY_ID

    summary = next((m for m in result.measurements if m.segment_id == ROUTE_SUMMARY_ID), None)

    confidences = [m.confidence for m in result.measurements]
    return {
        "measured_segments": len(result.measurements),
        "summary": (
            {key: _round(value, SEVERITY_DP) for key, value in sorted(summary.values.items())}
            if summary
            else None
        ),
        "mean_confidence": (
            round(sum(confidences) / len(confidences), SEVERITY_DP) if confidences else None
        ),
        "flags": {
            "hard": sum(1 for f in result.flags if f.kind is FlagKind.HARD),
            "soft": sum(1 for f in result.flags if f.kind is FlagKind.SOFT),
        },
        "worst": [
            {
                "segment_id": flag.segment_id,
                "severity": round(flag.severity, SEVERITY_DP),
                "reason_code": flag.reason_code,
                "kind": flag.kind.name.lower(),
                "tier": flag.tier.name.lower(),
            }
            for flag in result.worst(WORST_N)
        ],
    }


def measurements_hash(plan: Plan) -> str:
    """A hash over every measured value on every segment, of every scorer.

    The backstop for everything the summary leaves out. Sorted throughout so that a
    reordering — of scorers, of segments, of the keys inside one measurement — is not
    mistaken for a change in what was measured.
    """
    rows = [
        [
            result.name,
            measurement.segment_id,
            sorted((key, _round(value, HASH_DP)) for key, value in measurement.values.items()),
            round(measurement.confidence, HASH_DP),
        ]
        for result in sorted(plan.results, key=lambda r: r.name)
        for measurement in sorted(result.measurements, key=lambda m: m.segment_id)
    ]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest(plan: Plan) -> dict[str, Any]:
    """The full expectation for one plan."""
    duration_s = (plan.etas[-1] - plan.etas[0]).total_seconds() if len(plan.etas) > 1 else None
    return {
        "route": {
            "length_m": round(plan.route.length_m, QUANTITY_DP),
            "point_count": len(plan.route.points),
            "segment_count": len(plan.segments),
            "segments_matched_to_a_way": sum(1 for s in plan.segments if s.way_id is not None),
        },
        "elevation": (
            {
                "gain_m": round(plan.elevation.gain_m, QUANTITY_DP),
                "loss_m": round(plan.elevation.loss_m, QUANTITY_DP),
                "min_m": (
                    None
                    if plan.elevation.min_ele_m is None
                    else round(plan.elevation.min_ele_m, QUANTITY_DP)
                ),
                "max_m": (
                    None
                    if plan.elevation.max_ele_m is None
                    else round(plan.elevation.max_ele_m, QUANTITY_DP)
                ),
                "samples_missing": plan.elevation.samples_missing,
            }
            if plan.elevation
            else None
        ),
        "pacing": {
            "duration_s": round(duration_s, QUANTITY_DP) if duration_s is not None else None,
            # To the second, never finer. A microsecond is the seventh significant digit
            # of a half-hour ETA - it is f64 noise, and pinning it would fail this golden
            # on a different libm rather than on a change to the pacing model.
            "finish": (plan.etas[-1].replace(microsecond=0).isoformat() if plan.etas else None),
        },
        "scorers": {
            result.name: _scorer_digest(result)
            for result in sorted(plan.results, key=lambda r: r.name)
        },
        "residual_flags": [
            {
                "segment_id": flag.segment_id,
                "scorer": flag.scorer,
                "reason_code": flag.reason_code,
                "kind": flag.kind.name.lower(),
                "tier": flag.tier.name.lower(),
                "severity": round(flag.severity, SEVERITY_DP),
            }
            for flag in plan.residual_flags
        ],
        "verify": [
            {
                "number": check.number,
                "name": check.name,
                "status": check.status,
                "offenders": len(check.offenders),
            }
            for check in (plan.verify.results if plan.verify else [])
        ],
        "coverage": [
            {
                "source": entry.source,
                "kind": entry.kind,
                "checked": entry.checked,
                "reason": entry.reason,
                "vintage": entry.vintage,
            }
            for entry in plan.coverage.entries
        ],
        "measurements_sha256": measurements_hash(plan),
    }


def serialize(expectation: dict[str, Any]) -> str:
    """The on-disk form: stable key order, one value per line, trailing newline.

    Stable ordering is what makes an `expected.json` diff a review artifact rather than
    noise — a reordered dict would rewrite the whole file on an unrelated change.
    """
    return json.dumps(expectation, indent=2, sort_keys=True) + "\n"


def describe_difference(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """A unified diff of the two expectations, for the failure message."""
    import difflib

    return "".join(
        difflib.unified_diff(
            serialize(expected).splitlines(keepends=True),
            serialize(actual).splitlines(keepends=True),
            fromfile="expected.json",
            tofile="actual",
            n=2,
        )
    )
