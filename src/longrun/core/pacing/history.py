"""Run history becomes a pacing curve and an accepted-road set (scope 6.2).

Three things kept apart on purpose, because only the first is format-specific and only the
last needs a router:

* **reading** - FIT files, or a Strava bulk export's `activities.csv` plus its originals;
* **deriving** - a grade-adjusted pace curve, fatigue drift, a surface factor;
* **map matching** - the accepted-road set, which needs real `osm_way_id` values and
  therefore a router (ADR 0001 retired risk R4 on exactly this, and struck the
  geometry-hashing fallback).

Split that way, the arithmetic is testable with hand-built activities whose answer is known
by construction, and a reader for a new format is a reader rather than a second derivation.

**Local-first, and it is enforced here rather than promised** (scope 3.7): raw files are
read and never copied, only derived curves are returned, and the first and last
`PRIVACY_TRIM_M` of every activity are dropped before anything is matched or binned -
activities start at homes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import TYPE_CHECKING, Any

from longrun.core.pacing.curves import PacingCurves

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from longrun.core.routing.base import Router

#: Scope 6.2: "first/last 500 m of each activity stripped before map-matching". A privacy
#: measure before it is a data-quality one - a run starts where somebody lives.
PRIVACY_TRIM_M = 500.0

#: Scope 6.2 bins by 2% of grade, and `PacingCurves.speed_ms` looks the bin up by the key
#: this builds. Bin any other way and every lookup misses silently, falling back to
#: Minetti - which is legal, because a missing bin is legal.
GRADE_BIN_PCT = 2

#: A bin with fewer samples than this is noise, not a measurement.
MIN_SAMPLES_PER_BIN = 20

#: Scope 6.2: "accepted-road set: map-matched OSM ways run >=2 times (prior, not
#: constraint)".
MIN_RUNS_FOR_ACCEPTED = 2

#: Below this, a point pair is a pause rather than a stride, and scope 6.2 excludes
#: stopped time from the pace curve.
MOVING_SPEED_MS = 0.5


@dataclass(frozen=True)
class TrackPoint:
    """One sample from one activity."""

    lat: float
    lon: float
    cum_dist_m: float
    at: datetime
    ele_m: float | None = None


@dataclass(frozen=True)
class Activity:
    """One run, as read from a file and before anything is derived from it."""

    activity_id: str
    points: list[TrackPoint] = field(default_factory=list)
    sport: str | None = None
    #: Scope 6.2: "race efforts excluded or flagged". Whatever the source says - Strava's
    #: `activities.csv` has a workout type, FIT has a sub-sport - and never inferred from
    #: the pace, which would exclude somebody's good day.
    is_race: bool = False
    name: str | None = None

    @property
    def distance_m(self) -> float:
        return self.points[-1].cum_dist_m if self.points else 0.0


@dataclass(frozen=True)
class History:
    """What a set of activities produced, and what it could not."""

    curves: PacingCurves
    accepted_ways: frozenset[int] = frozenset()
    activities_read: int = 0
    races_excluded: int = 0
    reasons: list[str] = field(default_factory=list)


# --- deriving -----------------------------------------------------------------


def grade_bin(gradient: float) -> str:
    """The key `PacingCurves.speed_ms` looks a measured bin up by.

    Signed, even, unpadded, floored toward -inf: a fraction in, a percent lower bound out.
    Copied from the consumer rather than reinvented, because the two must agree exactly and
    a mismatch is invisible - the lookup just misses.
    """
    return f"{int(gradient * 100 // GRADE_BIN_PCT) * GRADE_BIN_PCT:+d}"


def derive(activities: Sequence[Activity], *, min_samples: int = MIN_SAMPLES_PER_BIN) -> History:
    """Turn activities into curves. No files, no network, no router."""
    usable = [a for a in activities if not a.is_race and len(a.points) > 1]
    races = sum(1 for a in activities if a.is_race)
    reasons: list[str] = []

    if races:
        # Flagged, not silently dropped: scope 6.2 allows either and the caveat is what
        # makes the number honest on the sheet.
        reasons.append(f"{races} race effort(s) excluded: a race is not the pace of a training run")
    if not usable:
        reasons.append("no usable activities: nothing with two or more track points")
        return History(curves=PacingCurves(), activities_read=len(activities), reasons=reasons)

    samples: dict[str, list[float]] = {}
    flat: list[float] = []
    for activity in usable:
        for gradient, speed in _strides(activity):
            samples.setdefault(grade_bin(gradient), []).append(speed)
            if abs(gradient) < 0.01:
                flat.append(speed)

    measured = {
        key: round(median(values), 4)
        for key, values in samples.items()
        if len(values) >= min_samples
    }
    thin = sorted(key for key, values in samples.items() if len(values) < min_samples)
    if thin:
        reasons.append(
            f"{len(thin)} grade bin(s) had fewer than {min_samples} samples and fall back "
            f"to the population curve: {', '.join(thin)}"
        )

    longest = max(a.distance_m for a in usable)
    flat_speed = median(flat) if len(flat) >= min_samples else None
    if flat_speed is None:
        reasons.append(
            "no flat-ground samples: the population flat speed stands, and every "
            "unmeasured grade bin is scaled from it"
        )

    return History(
        curves=PacingCurves(
            flat_speed_ms=flat_speed if flat_speed else PacingCurves().flat_speed_ms,
            provenance="history",
            longest_effort_m=longest,
            speed_by_grade_bin=measured,
            fatigue_drift_pct_per_10km=_drift(usable),
        ),
        activities_read=len(activities),
        races_excluded=races,
        reasons=reasons,
    )


def _strides(activity: Activity) -> Iterable[tuple[float, float]]:
    """Gradient and speed for each moving step, with the ends trimmed."""
    points = _trimmed(activity.points)
    for before, after in zip(points, points[1:], strict=False):
        run = after.cum_dist_m - before.cum_dist_m
        seconds = (after.at - before.at).total_seconds()
        if run <= 0 or seconds <= 0:
            continue
        speed = run / seconds
        if speed < MOVING_SPEED_MS:
            # Stopped time, which scope 6.2 excludes: a traffic light is not a pace.
            continue
        rise = (after.ele_m or 0.0) - (before.ele_m or 0.0) if before.ele_m is not None else 0.0
        yield rise / run, speed


def _trimmed(points: list[TrackPoint]) -> list[TrackPoint]:
    """Scope 6.2's 500 m off each end, before anything looks at where it was."""
    if not points:
        return []
    total = points[-1].cum_dist_m
    if total <= 2 * PRIVACY_TRIM_M:
        # A run shorter than the trim is all doorstep. Dropping it entirely is the honest
        # answer: there is nothing left that is not somebody's address.
        return []
    return [p for p in points if PRIVACY_TRIM_M <= p.cum_dist_m <= total - PRIVACY_TRIM_M]


def _drift(activities: Sequence[Activity]) -> float:
    """Percent slower per 10 km, from the longest efforts.

    Measured as first-quarter against last-quarter speed on the longest runs, because that
    is where drift shows and a 5 km run has none to show.
    """
    longest = sorted(activities, key=lambda a: a.distance_m, reverse=True)[:5]
    drifts: list[float] = []
    for activity in longest:
        speeds = [speed for _, speed in _strides(activity)]
        if len(speeds) < 8 or activity.distance_m < 1000:
            continue
        quarter = max(1, len(speeds) // 4)
        early, late = median(speeds[:quarter]), median(speeds[-quarter:])
        if early <= 0:
            continue
        per_10km = (1 - late / early) * 100 * (10_000 / activity.distance_m)
        drifts.append(max(0.0, per_10km))
    return round(median(drifts), 2) if drifts else PacingCurves().fatigue_drift_pct_per_10km


# --- the accepted-road set ------------------------------------------------------


def accepted_ways(
    activities: Sequence[Activity], router: Router, *, min_runs: int = MIN_RUNS_FOR_ACCEPTED
) -> tuple[frozenset[int], list[str]]:
    """Ways run at least `min_runs` times, keyed on real OSM way ids.

    ADR 0001 retired risk R4 by measuring that `POST /match` returns `osm_way_id` per edge,
    and struck the geometry-hashing fallback from the plan. This is that decision's
    implementation, and it is a *prior* rather than a constraint (scope 6.2).
    """
    from longrun.core.models.geometry import Route, RoutePoint

    counts: dict[int, int] = {}
    reasons: list[str] = []
    for activity in activities:
        points = _trimmed(activity.points)
        if len(points) < 2:
            continue
        route = Route(
            id=activity.activity_id,
            points=[
                RoutePoint(lat=p.lat, lon=p.lon, cum_dist_m=p.cum_dist_m - points[0].cum_dist_m)
                for p in points
            ],
        )
        try:
            _, way_ids = router.map_match(route)
        except Exception as exc:  # noqa: BLE001 - one bad activity is not the whole history
            reasons.append(f"{activity.activity_id}: {type(exc).__name__}")
            continue
        for way_id in {w for w in way_ids if w is not None}:
            counts[way_id] = counts.get(way_id, 0) + 1

    return frozenset(w for w, n in counts.items() if n >= min_runs), reasons


# --- reading ---------------------------------------------------------------------


def read_fit(path: Path) -> Activity | None:
    """One FIT file, or `None` when it holds no track.

    `fitdecode` is the `history` extra, so it is imported here rather than at module scope.
    """
    import fitdecode

    points: list[TrackPoint] = []
    sport: str | None = None
    sub_sport: str | None = None

    with fitdecode.FitReader(str(path)) as reader:
        for frame in reader:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "sport":
                sport = _field(frame, "sport")
                sub_sport = _field(frame, "sub_sport")
            elif frame.name == "record":
                point = _record(frame)
                if point is not None:
                    points.append(point)

    if not points:
        return None
    return Activity(
        activity_id=path.stem,
        points=points,
        sport=str(sport) if sport else None,
        is_race=str(sub_sport or "").lower() == "race",
    )


def _record(frame: Any) -> TrackPoint | None:
    lat, lon = _field(frame, "position_lat"), _field(frame, "position_long")
    at, distance = _field(frame, "timestamp"), _field(frame, "distance")
    if lat is None or lon is None or at is None or distance is None:
        # A record with no position is a heart-rate sample, not a place. Dropped rather
        # than interpolated: an invented position becomes an accepted road.
        return None
    return TrackPoint(
        lat=_semicircles(lat),
        lon=_semicircles(lon),
        cum_dist_m=float(distance),
        at=at,
        ele_m=_optional_float(_field(frame, "altitude")),
    )


def _field(frame: Any, name: str) -> Any:
    try:
        return frame.get_value(name)
    except (KeyError, AttributeError):
        return None


def _semicircles(value: Any) -> float:
    """FIT stores degrees as semicircles: 2**31 of them to 180 degrees.

    `fitdecode` converts the common ones already, so a plausible degree value is passed
    through - converting twice would put a Bay Area run in the Atlantic.
    """
    number = float(value)
    if -180.0 <= number <= 180.0:
        return number
    return number * (180.0 / 2**31)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ingest(activities: Sequence[Activity], *, router: Router | None = None) -> History:
    """Derive everything derivable, and the accepted-road set when there is a router."""
    history = derive(activities)
    if router is None:
        return History(
            curves=history.curves,
            activities_read=history.activities_read,
            races_excluded=history.races_excluded,
            reasons=[
                *history.reasons,
                "no router: the accepted-road set needs real OSM way ids from map matching "
                "(ADR 0001), so it is empty rather than guessed",
            ],
        )
    ways, problems = accepted_ways([a for a in activities if not a.is_race], router)
    return History(
        curves=history.curves,
        accepted_ways=ways,
        activities_read=history.activities_read,
        races_excluded=history.races_excluded,
        reasons=[*history.reasons, *problems],
    )


__all__ = [
    "GRADE_BIN_PCT",
    "MIN_RUNS_FOR_ACCEPTED",
    "MIN_SAMPLES_PER_BIN",
    "MOVING_SPEED_MS",
    "PRIVACY_TRIM_M",
    "Activity",
    "History",
    "TrackPoint",
    "accepted_ways",
    "derive",
    "grade_bin",
    "ingest",
    "read_fit",
]
