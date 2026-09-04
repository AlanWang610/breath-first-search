"""The ten `gpx_verify` checks (scope 7.9).

Each returns structured offenders rather than a boolean, because scope 8.1 step 9 sends a
failure back to rerouting with the failing segment — a bare False would leave the loop
nothing to act on.

A check that cannot run reports `skipped`, never `passed`. That distinction is the whole
point: check 6 needs a closures result and check 9 needs time constraints in the request,
and reporting either as a pass because its input was missing would be exactly the silent
false assurance scope 3.6 exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from longrun.core.geo.dem import SPIKE_THRESHOLD_M
from longrun.core.geo.gpx import haversine_m
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.request import LockedRange, TimeConstraints

CheckStatus = Literal["passed", "failed", "skipped"]

#: Scope 7.9 check 3: a gap larger than this means the track is not continuous.
MAX_POINT_GAP_M = 100.0

#: Scope 7.9 check 2: a trackpoint further than this from a routable way is off-network.
MAX_SNAP_DISTANCE_M = 15.0

#: Scope 7.9 check 8.
CROSSING_SPEED_LIMIT_KPH = 40.0


class CheckResult(BaseModel):
    """One numbered check's outcome."""

    number: int
    name: str
    status: CheckStatus
    offenders: list[str] = Field(default_factory=list)
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def blocking(self) -> bool:
        """Only an outright failure blocks; a skip is reported, not fatal."""
        return self.status == "failed"


def _result(
    number: int,
    name: str,
    offenders: list[str],
    detail: str | None = None,
) -> CheckResult:
    return CheckResult(
        number=number,
        name=name,
        status="failed" if offenders else "passed",
        offenders=offenders,
        detail=detail,
    )


def _skip(number: int, name: str, reason: str) -> CheckResult:
    return CheckResult(number=number, name=name, status="skipped", detail=reason)


def check_1_valid_gpx(route: Route) -> CheckResult:
    """Valid GPX 1.1 with a usable track."""
    offenders = []
    if len(route.points) < 2:
        offenders.append("route has fewer than two points")
    return _result(1, "valid_gpx", offenders)


def check_2_on_network(route: Route, snapped_distances_m: list[float] | None) -> CheckResult:
    """Every trackpoint within 15 m of a routable way with foot != no."""
    if snapped_distances_m is None:
        return _skip(2, "on_network", "no map-matching result available")
    offenders = [
        f"point {i} is {d:.0f} m from the nearest routable way"
        for i, d in enumerate(snapped_distances_m)
        if d > MAX_SNAP_DISTANCE_M
    ]
    return _result(2, "on_network", offenders)


def check_3_no_gaps(route: Route) -> CheckResult:
    """No inter-point gap greater than 100 m."""
    offenders = []
    for i in range(1, len(route.points)):
        a, b = route.points[i - 1], route.points[i]
        gap = haversine_m(a.lat, a.lon, b.lat, b.lon)
        if gap > MAX_POINT_GAP_M:
            offenders.append(f"{gap:.0f} m gap between points {i - 1} and {i}")
    return _result(3, "no_gaps", offenders)


def check_4_elevation_sane(elevations: list[float | None] | None) -> CheckResult:
    """Elevation consistent with the DEM: no interpolation artifacts, no spikes."""
    if elevations is None:
        return _skip(4, "elevation_sane", "no elevation sampled")
    offenders = []
    for i in range(1, len(elevations) - 1):
        prev, cur, nxt = elevations[i - 1], elevations[i], elevations[i + 1]
        if prev is None or cur is None or nxt is None:
            continue
        if (
            abs(cur - prev) > SPIKE_THRESHOLD_M
            and abs(nxt - cur) > SPIKE_THRESHOLD_M
            and abs(nxt - prev) < SPIKE_THRESHOLD_M
        ):
            offenders.append(f"elevation spike at point {i}: {cur:.0f} m")
    return _result(4, "elevation_sane", offenders)


def check_5_no_illegal_ways(legality_offenders: list[str] | None) -> CheckResult:
    """No access=private, motorway, or railway ROW segments."""
    if legality_offenders is None:
        return _skip(5, "no_illegal_ways", "legality scorer did not run")
    return _result(5, "no_illegal_ways", list(legality_offenders))


def check_6_no_active_closures(closure_offenders: list[str] | None) -> CheckResult:
    """No overlap with active closures.

    Skipped whenever the closures scorer had no adapter to consult — reporting a pass
    would claim the route was checked against closure data that was never fetched.
    """
    if closure_offenders is None:
        return _skip(6, "no_active_closures", "no closure data available for this route")
    return _result(6, "no_active_closures", list(closure_offenders))


def check_7_distance_in_tolerance(
    route: Route, target_km: float | None, tolerance_pct: float
) -> CheckResult:
    """Total distance within tolerance of the target."""
    if target_km is None:
        return _skip(7, "distance_in_tolerance", "no target distance requested")
    actual_km = route.length_m / 1000.0
    slack = target_km * tolerance_pct / 100.0
    if abs(actual_km - target_km) > slack:
        return _result(
            7,
            "distance_in_tolerance",
            [f"{actual_km:.1f} km against a {target_km:.1f} km target (±{slack:.1f})"],
        )
    return _result(7, "distance_in_tolerance", [])


def check_8_no_dangerous_crossings(crossing_offenders: list[str] | None) -> CheckResult:
    """No unsignalized crossing of trunk/primary with maxspeed > 40."""
    if crossing_offenders is None:
        return _skip(8, "no_dangerous_crossings", "crossings scorer did not run")
    return _result(8, "no_dangerous_crossings", list(crossing_offenders))


def check_9_time_constraints(
    constraints: TimeConstraints, etas: list[datetime] | None
) -> CheckResult:
    """No hard time-constraint violation at projected pace."""
    if etas is None or not etas:
        return _skip(9, "time_constraints", "no ETAs available")
    if (
        constraints.arrive_by is None
        and constraints.earliest_start is None
        and constraints.total_budget is None
    ):
        return _skip(9, "time_constraints", "no time constraints requested")

    offenders = []
    start, finish = etas[0], etas[-1]
    if constraints.arrive_by and finish > constraints.arrive_by:
        late = (finish - constraints.arrive_by).total_seconds() / 60
        offenders.append(f"finishes {late:.0f} min after arrive-by")
    if constraints.earliest_start and start < constraints.earliest_start:
        early = (constraints.earliest_start - start).total_seconds() / 60
        offenders.append(f"starts {early:.0f} min before the earliest permitted start")
    if constraints.total_budget and (finish - start) > constraints.total_budget:
        over = ((finish - start) - constraints.total_budget).total_seconds() / 60
        offenders.append(f"exceeds the time budget by {over:.0f} min")
    return _result(9, "time_constraints", offenders)


def check_10_locks_intact(
    segments: list[Segment], locked: list[LockedRange], original: Route | None
) -> CheckResult:
    """Locked segments unchanged from the locked geometry."""
    if original is None:
        return _skip(10, "locks_intact", "no pre-edit route to compare against")
    if not locked:
        return _result(10, "locks_intact", [])

    offenders = []
    for lock in locked:
        for point in original.points:
            if lock.start_m <= point.cum_dist_m <= lock.end_m:
                break
        else:
            offenders.append(
                f"locked range {lock.start_m:.0f}-{lock.end_m:.0f} m is no longer on the route"
            )
    return _result(10, "locks_intact", offenders)
