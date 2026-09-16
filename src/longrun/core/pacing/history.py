"""Run history becomes a pacing curve and an accepted-road set (scope 6.2).

Three things kept apart on purpose, because only the first is format-specific and only the
last needs a router:

* **reading** - FIT and GPX, either of them gzipped, from a path or straight from bytes.
  A Strava bulk export is a reader of its own (`strava.py`), because what decides which of
  its files are runs is `activities.csv` rather than anything in the files;
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

import functools
import gzip
import io
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import median
from typing import IO, TYPE_CHECKING, Any, Literal

from longrun.core.geo.dem import DEFAULT_SMOOTH_M
from longrun.core.geo.gpx import haversine_m
from longrun.core.pacing.curves import PacingCurves

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    from longrun.core.models.geometry import Route
    from longrun.core.routing.base import Router

ElevationSource = Literal["terrain", "device"]

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

#: Shortest stretch of moving a speed and a gradient are measured over: the window a plan
#: grades its own route over, so a bin means the same grade in both places. Point to point,
#: which is what this measured before, a watch recording every 1-7 s gives strides of a few
#: metres, and a real archive put 8% of them in bins steeper than 20% - elevation noise
#: divided by a short run, not hills.
STRIDE_M = DEFAULT_SMOOTH_M

#: Sports whose speed is a runner's pace, as FIT's enum and a GPX `<type>` spell them.
#: Everything else - a ride, a walk, a swim, a treadmill's `virtual_run` - is excluded
#: before a single stride is binned.
RUNNING_SPORTS = frozenset({"running", "trail_running"})


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
    #: Lower-case, as FIT's enum and a GPX `<type>` both spell it (`running`, `cycling`).
    #: `derive` excludes anything that names a sport outside `RUNNING_SPORTS`, because any
    #: bulk export mixes activity types and a cycling median near 8 m/s would otherwise
    #: become the runner's flat speed. `None` - a file that does not say - is read as a run
    #: and counted in the reasons, since a hand-picked file is usually one.
    sport: str | None = None
    #: Scope 6.2: "race efforts excluded or flagged". Whatever the source says - FIT has a
    #: sub-sport; a real Strava export turned out to carry no marker at all - and never
    #: inferred from the pace, which would exclude somebody's good day.
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


def is_running(activity: Activity) -> bool:
    """Whether an activity's speed may be binned as pace: it says running, or says nothing."""
    return activity.sport is None or activity.sport in RUNNING_SPORTS


def derive(
    activities: Sequence[Activity],
    *,
    min_samples: int = MIN_SAMPLES_PER_BIN,
    stride_m: float = STRIDE_M,
    elevation: ElevationSource = "device",
) -> History:
    """Turn activities into curves. No files, no network, no router.

    `elevation` says where the points' `ele_m` came from - the device, or `with_terrain` -
    and is recorded on the curve, because it decides whether the grade bins can be trusted.
    """
    runs = [a for a in activities if is_running(a)]
    usable = [a for a in runs if not a.is_race and len(a.points) > 1]
    races = sum(1 for a in runs if a.is_race)
    reasons: list[str] = []

    not_running = Counter(a.sport for a in activities if not is_running(a))
    if not_running:
        listed = ", ".join(f"{sport} {count}" for sport, count in not_running.most_common())
        reasons.append(
            f"{sum(not_running.values())} activity(ies) excluded as not running ({listed}): "
            "a ride's speed is not a runner's pace"
        )
    unlabelled = sum(1 for a in runs if a.sport is None)
    if unlabelled and unlabelled < len(activities):
        reasons.append(f"{unlabelled} activity(ies) name no sport and were binned as running")

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
        for gradient, speed in _strides(activity, stride_m=stride_m):
            if gradient is None:
                # No elevation at one end: a speed with no grade to file it under.
                continue
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
            grade_elevation=elevation if measured else None,
            fatigue_drift_pct_per_10km=_drift(usable, stride_m=stride_m),
        ),
        activities_read=len(activities),
        races_excluded=races,
        reasons=reasons,
    )


def _strides(
    activity: Activity, *, stride_m: float = STRIDE_M
) -> Iterable[tuple[float | None, float]]:
    """Gradient and speed over each stretch of at least `stride_m` of moving, ends trimmed.

    A stop ends a stretch and the next begins where the runner set off again, so stopped
    time never reaches a speed. The gradient is `None` when either end has no elevation.
    """
    points = _trimmed(activity.points)
    if not points:
        return
    start = points[0]
    for before, after in zip(points, points[1:], strict=False):
        seconds = (after.at - before.at).total_seconds()
        if seconds <= 0:
            continue
        if (after.cum_dist_m - before.cum_dist_m) / seconds < MOVING_SPEED_MS:
            # Stopped time, which scope 6.2 excludes: a traffic light is not a pace.
            start = after
            continue
        span = after.cum_dist_m - start.cum_dist_m
        if span <= 0 or span < stride_m:
            continue
        elapsed = (after.at - start.at).total_seconds()
        gradient = (
            (after.ele_m - start.ele_m) / span
            if after.ele_m is not None and start.ele_m is not None
            else None
        )
        yield gradient, span / elapsed
        start = after


def _trimmed(points: list[TrackPoint]) -> list[TrackPoint]:
    """Scope 6.2's 500 m off each end, before anything looks at where it was."""
    return [points[i] for i in _kept(points)]


def _kept(points: list[TrackPoint]) -> list[int]:
    """Indices of the points that survive the trim."""
    if not points:
        return []
    total = points[-1].cum_dist_m
    if total <= 2 * PRIVACY_TRIM_M:
        # A run shorter than the trim is all doorstep. Dropping it entirely is the honest
        # answer: there is nothing left that is not somebody's address.
        return []
    return [
        i for i, p in enumerate(points) if PRIVACY_TRIM_M <= p.cum_dist_m <= total - PRIVACY_TRIM_M
    ]


def with_terrain(
    activities: Sequence[Activity], sample: Callable[[Route], list[float | None]]
) -> tuple[list[Activity], int]:
    """Each activity's elevation replaced by terrain elevation, over the trimmed range only.

    Scope 7.1 grades a plan on the terrain model and never on a GPX, and a pace curve is
    looked up by that grade - so bins measured on a watch's altimeter are applied to a
    different quantity than they measured. `sample` is `dem.sample_elevation` over whatever
    store covers the route, injected so this module never picks a raster source.

    Only the trimmed points are asked about. The ends get `None`: nothing reads them, and a
    terrain lookup is a question about where somebody was. Returns the activities and how
    many had no terrain coverage at all.
    """
    from longrun.core.models.geometry import Route, RoutePoint

    out: list[Activity] = []
    uncovered = 0
    for activity in activities:
        kept = _kept(activity.points)
        values: list[float | None] = []
        if len(kept) >= 2:
            origin = activity.points[kept[0]].cum_dist_m
            try:
                route = Route(
                    id=activity.activity_id,
                    points=[
                        RoutePoint(
                            lat=activity.points[i].lat,
                            lon=activity.points[i].lon,
                            cum_dist_m=activity.points[i].cum_dist_m - origin,
                        )
                        for i in kept
                    ],
                )
                values = sample(route)
            except ValueError:
                # A track whose distance runs backwards is not a route. Its grades are
                # unknown, which is what an empty sample says.
                values = []
        if not any(v is not None for v in values):
            uncovered += 1
        terrain = dict(zip(kept, values, strict=False))
        points = [replace(p, ele_m=terrain.get(i)) for i, p in enumerate(activity.points)]
        out.append(replace(activity, points=points))
    return out, uncovered


def _drift(activities: Sequence[Activity], *, stride_m: float = STRIDE_M) -> float:
    """Percent slower per 10 km, from the longest efforts.

    Measured as first-quarter against last-quarter speed on the longest runs, because that
    is where drift shows and a 5 km run has none to show.
    """
    longest = sorted(activities, key=lambda a: a.distance_m, reverse=True)[:5]
    drifts: list[float] = []
    for activity in longest:
        speeds = [speed for _, speed in _strides(activity, stride_m=stride_m)]
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


#: The formats a real Strava archive holds its runs in: `.fit.gz` for nearly everything a
#: device recorded, `.gpx` for app recordings, and a few plain `.fit` and `.gpx.gz`. The
#: archive's one `.tcx.gz` was a swim, so TCX has no reader - a TCX run is reported as a
#: format this does not decode, never guessed at.
READABLE_SUFFIXES = (".fit", ".fit.gz", ".gpx", ".gpx.gz")


class UnsupportedActivityFile(ValueError):
    """A file in a format this module does not decode."""


def read_activity(name: str, data: bytes) -> Activity | None:
    """One activity file, by its name and its bytes. `None` when it holds no track.

    Bytes rather than a path, so an export is read straight out of its zip: scope 3.7's
    "read where they sit, nothing copied" stays true of an archive that also holds
    somebody's messages, contacts and login history.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".gz"):
        data = gzip.decompress(data)
        base = base[:-3]
    activity_id = base.split(".", 1)[0]
    if base.lower().endswith(".fit"):
        return _read_fit(io.BytesIO(data), activity_id)
    if base.lower().endswith(".gpx"):
        return _read_gpx(data, activity_id)
    raise UnsupportedActivityFile(f"{base}: not a FIT or GPX file")


def read_fit(path: Path) -> Activity | None:
    """One FIT file (or `.fit.gz`) from disk, or `None` when it holds no track."""
    return read_activity(path.name, path.read_bytes())


def _read_fit(stream: IO[bytes], activity_id: str) -> Activity | None:
    """`fitdecode` is the `history` extra, so it is imported here rather than at module scope."""
    import fitdecode

    points: list[TrackPoint] = []
    sport: Any = None
    sub_sport: Any = None

    with _fit_reader()(stream, error_handling=fitdecode.ErrorHandling.IGNORE) as reader:
        for frame in reader:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name in ("sport", "session"):
                # Devices write both; `sport` comes first, `session` is the fallback.
                if sport is None:
                    sport = _field(frame, "sport")
                    sub_sport = _field(frame, "sub_sport")
            elif frame.name == "record":
                point = _record(frame)
                if point is not None:
                    points.append(point)

    if not points:
        return None
    return Activity(
        activity_id=activity_id,
        points=points,
        sport=_sport(sport),
        is_race=str(sub_sport or "").lower() == "race",
    )


@functools.cache
def _fit_reader() -> Any:
    """`fitdecode.FitReader`, less one crash that a real device file found.

    fitdecode 0.11.0 raises `developer_data_index N not defined` when a `field_description`
    names a developer data index no `developer_data_id` message declared - and raises it
    unconditionally, even under `ErrorHandling.IGNORE`. Its own lookup path (`_get_dev_type`)
    meets the same absence under IGNORE by registering a placeholder; this does that one
    step earlier. The fields in question belong to a third-party watch app and carry nothing
    this module reads, so without the override a whole run is lost to a field it ignores.
    """
    import fitdecode

    class _Reader(fitdecode.FitReader):  # type: ignore[misc]
        def _add_dev_field_description(self, message: Any) -> Any:
            try:
                index = message.get_raw_value("developer_data_index")
            except KeyError:
                index = None
            if index is not None and int(index) not in self._local_dev_types:
                self._add_dev_data_id_impl(int(index))
            return super()._add_dev_field_description(message)

    return _Reader


def _read_gpx(data: bytes, activity_id: str) -> Activity | None:
    """Track points with a time. A point without one is shape, and shape is not pace."""
    import gpxpy

    parsed = gpxpy.parse(data.decode("utf-8-sig"))
    points: list[TrackPoint] = []
    for track in parsed.tracks:
        for segment in track.segments:
            for p in segment.points:
                if p.time is None:
                    continue
                total = (
                    points[-1].cum_dist_m
                    + haversine_m(points[-1].lat, points[-1].lon, p.latitude, p.longitude)
                    if points
                    else 0.0
                )
                points.append(
                    TrackPoint(
                        lat=p.latitude,
                        lon=p.longitude,
                        cum_dist_m=total,
                        at=p.time,
                        ele_m=p.elevation,
                    )
                )
    if not points:
        return None
    return Activity(
        activity_id=activity_id,
        points=points,
        sport=_sport(next((t.type for t in parsed.tracks if t.type), None)),
    )


def _sport(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip().lower() or None


def _record(frame: Any) -> TrackPoint | None:
    lat, lon = _field(frame, "position_lat"), _field(frame, "position_long")
    at, distance = _field(frame, "timestamp"), _field(frame, "distance")
    if lat is None or lon is None or at is None or distance is None:
        # A record with no position is a heart-rate sample, not a place. Dropped rather
        # than interpolated: an invented position becomes an accepted road.
        return None
    # `enhanced_altitude` first: current devices write it on every record, and 16-bit
    # `altitude` is the older field some omit.
    altitude = _field(frame, "enhanced_altitude")
    return TrackPoint(
        lat=_semicircles(lat),
        lon=_semicircles(lon),
        cum_dist_m=float(distance),
        at=at,
        ele_m=_optional_float(altitude if altitude is not None else _field(frame, "altitude")),
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


def ingest(
    activities: Sequence[Activity],
    *,
    router: Router | None = None,
    elevation: ElevationSource = "device",
) -> History:
    """Derive everything derivable, and the accepted-road set when there is a router."""
    history = derive(activities, elevation=elevation)
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
    ways, problems = accepted_ways(
        [a for a in activities if is_running(a) and not a.is_race], router
    )
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
    "READABLE_SUFFIXES",
    "RUNNING_SPORTS",
    "STRIDE_M",
    "Activity",
    "ElevationSource",
    "History",
    "TrackPoint",
    "UnsupportedActivityFile",
    "accepted_ways",
    "derive",
    "grade_bin",
    "ingest",
    "is_running",
    "read_activity",
    "read_fit",
    "with_terrain",
]
