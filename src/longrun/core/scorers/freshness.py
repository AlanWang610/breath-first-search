"""Which scorers a refresh must re-run, and which it may carry (scope 7.8).

Scope 7.8 asks for a refresh that re-scores "time-dependent" scorers against a new date and
reuses everything else from cache. This is the list that makes "time-dependent" a decision
somebody took rather than a word in a sentence.

The shape is `core/plan/weighting.py`'s, deliberately, and for the reason its own comment
gives: *"an unlisted scorer is indistinguishable from one someone decided about, and
`trail_status` sat in that state from M1 until M4 found it."* Two exhaustive frozensets and
a test that fails on an unclassified scorer, rather than a default that silently carries a
scorer nobody thought about - which on a refresh means reporting last month's answer as this
week's.

**Broader than the scope's own parenthetical.** Scope 7.8 names "closures, weather, AQ,
trail status", which is illustrative rather than complete: `lighting` is solar elevation
against civil twilight, `transit` is a service-day question, `bailouts` reachability depends
on whether a stop is running at the segment's ETA, and `start_time_optimizer` is anchored on
the requested start. All four change with the date, and a refresh that carried them would
report last week's last train.
"""

from __future__ import annotations

from longrun.core.scorers.registry import closure

#: Scorers whose answer changes when the date or the start time changes. One line each,
#: naming what the time enters through - the reading is the artefact here, not the set.
TIME_DEPENDENT = frozenset(
    {
        "microclimate",  # the forecast itself, keyed on etas[0].date()
        "sun_exposure",  # solar geometry at each segment's arrival
        "heat_stress",  # WBGT at arrival; reads sun_exposure through `prior`
        "lighting",  # solar elevation against civil twilight
        "air_quality",  # hourly AQI at arrival
        "resupply_schedule",  # opening_hours at arrival; reads heat_stress through `prior`
        "hazards",  # seasonal snow only, via etas[0].date() - but that is enough
        "closures",  # adapter fetch on the clock's date; flag timing on the ETA
        "trail_status",  # adapter fetch on the clock's date
        "access_hours",  # adapter fetch on the clock's date; gates open at an hour
        "transit",  # service span for that day
        "bailouts",  # reachability by transit, which is a service-day question
        "crew_points",  # the runner ETA string only - but that string is on the sheet
        "start_time_optimizer",  # the whole sweep is anchored on the requested start
    }
)

#: Scorers whose answer is a property of the ground rather than of the day.
#:
#: `test_freshness.py` proves this rather than trusting it: none of these may name `etas`
#: outside its own signature or reach `ctx.clock`. The claim is cheap to make and was worth
#: earning - it is the half of the classification that decides what a refresh *skips*, and a
#: scorer wrongly listed here is one a refresh would never run again.
TIME_INDEPENDENT = frozenset(
    {
        "legality",
        "segment_hostility",
        "crossings",
        "stop_density",
        "surface_profile",
        "services_along",
        "cell_coverage",
    }
)


def rescore_set() -> frozenset[str]:
    """What a refresh re-runs: the time-dependent scorers, plus anything they read.

    Closed over `PRIOR_DEPENDENCIES` so the set handed to `run_scorers` is one it will
    accept. It is already closed today - `sun_exposure` and `heat_stress` are both
    time-dependent - and the closure is here for the day a `prior` edge crosses the
    boundary, which is exactly when nobody would think to check.
    """
    return closure(TIME_DEPENDENT)


__all__ = ["TIME_DEPENDENT", "TIME_INDEPENDENT", "rescore_set"]
