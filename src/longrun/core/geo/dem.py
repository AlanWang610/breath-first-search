"""Elevation from the DEM (scope 7.1, 7.9).

Elevation comes from the terrain model, never from the GPX. Consumer-device barometric
elevation drifts, and GPX files from planning tools frequently carry interpolated or
absent elevation — scope 7.9 check 4 exists precisely because of that. Sampling 3DEP
gives every plan the same elevation basis, which is what makes gain figures comparable
between two plans and between two users.

Grades are computed on a smoothed profile. Raw point-to-point grade over a 10 m DEM and
5 m GPS spacing is dominated by sampling noise: a 1 m vertical error across 5 m of
travel reads as 20%, which would flag a flat street as a wall.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.core.models.geometry import Route

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.data.base import RasterStore

#: Below this the profile is noise, not terrain; used for gain/loss accumulation.
DEFAULT_GAIN_THRESHOLD_M = 3.0

#: Grades are evaluated over at least this much travel (scope 7.1 sustained climbs).
DEFAULT_SMOOTH_M = 50.0

#: A step larger than this between adjacent samples is a DEM artifact, not terrain.
SPIKE_THRESHOLD_M = 30.0

#: Bin width for the grade histogram reported in the plan sheet (scope 9).
GRADE_BIN_PCT = 2.0


class SustainedRun(BaseModel):
    """A continuous climb or descent, as scope 7.1 and 8.3 report them."""

    start_m: float = Field(ge=0)
    end_m: float = Field(ge=0)
    gain_m: float
    mean_grade_pct: float

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


class ElevationProfile(BaseModel):
    """Gain, loss, grade distribution and the longest sustained runs."""

    gain_m: float = 0.0
    loss_m: float = 0.0
    min_ele_m: float | None = None
    max_ele_m: float | None = None
    grade_histogram: dict[str, float] = Field(default_factory=dict)
    longest_climb: SustainedRun | None = None
    longest_descent: SustainedRun | None = None
    samples_missing: int = 0

    @property
    def has_elevation(self) -> bool:
        return self.min_ele_m is not None


def sample_elevation(route: Route, rasters: RasterStore, layer: str = "dem") -> list[float | None]:
    """Elevation at every route point, `None` where the DEM has no coverage.

    Missing samples are left as gaps rather than zero-filled: a hole in the DEM is
    unknown elevation, and zero would read as sea level and invent thousands of metres
    of gain (scope 12).
    """
    from longrun.core.geo.projections import bbox_of

    window = rasters.read_window(layer, bbox_of(route, pad_deg=0.01))
    if window is None:
        return [None] * len(route.points)
    return _sample_window(route, window)


def _sample_window(route: Route, window: tuple[Any, Any]) -> list[float | None]:
    """Read one value per route point from an (array, affine transform) window."""
    array, transform = window
    inverse = ~transform
    out: list[float | None] = []
    rows, cols = array.shape[-2], array.shape[-1]
    band = array[0] if array.ndim == 3 else array
    for point in route.points:
        col, row = inverse * (point.lon, point.lat)
        r, c = int(row), int(col)
        if 0 <= r < rows and 0 <= c < cols:
            value = float(band[r, c])
            out.append(None if value != value else value)  # NaN check without numpy
        else:
            out.append(None)
    return out


def strip_spikes(
    elevations: list[float | None], threshold_m: float = SPIKE_THRESHOLD_M
) -> list[float | None]:
    """Replace single-sample jumps that exceed `threshold_m` and come straight back.

    This is the same defect scope 7.9 check 4 tests for; removing it here keeps a DEM
    artifact from being reported as several hundred metres of climbing.
    """
    if len(elevations) < 3:
        return list(elevations)

    out = list(elevations)
    for i in range(1, len(out) - 1):
        prev, cur, nxt = out[i - 1], out[i], out[i + 1]
        if prev is None or cur is None or nxt is None:
            continue
        if abs(cur - prev) > threshold_m and abs(nxt - cur) > threshold_m:
            if abs(nxt - prev) < threshold_m:
                out[i] = (prev + nxt) / 2.0
    return out


def grades(
    route: Route, elevations: list[float | None], smooth_m: float = DEFAULT_SMOOTH_M
) -> list[float | None]:
    """Grade in percent at every route point, smoothed over `smooth_m` of travel.

    The window is expressed in distance rather than point count so that a densely
    sampled track and a sparse one produce comparable grades.
    """
    n = len(route.points)
    if n != len(elevations):
        raise ValueError(f"got {len(elevations)} elevations for {n} route points")

    out: list[float | None] = []
    for i in range(n):
        j, k = _window_bounds(route, i, smooth_m)
        ele_a, ele_b = elevations[j], elevations[k]
        run = route.points[k].cum_dist_m - route.points[j].cum_dist_m
        if ele_a is None or ele_b is None or run <= 0:
            out.append(None)
        else:
            out.append((ele_b - ele_a) / run * 100.0)
    return out


def _window_bounds(route: Route, i: int, smooth_m: float) -> tuple[int, int]:
    """Indices bracketing point `i` across at least `smooth_m` where possible."""
    half = smooth_m / 2.0
    here = route.points[i].cum_dist_m
    j = i
    while j > 0 and here - route.points[j].cum_dist_m < half:
        j -= 1
    k = i
    last = len(route.points) - 1
    while k < last and route.points[k].cum_dist_m - here < half:
        k += 1
    return j, k


def elevation_profile(
    route: Route,
    elevations: list[float | None],
    gain_threshold_m: float = DEFAULT_GAIN_THRESHOLD_M,
    smooth_m: float = DEFAULT_SMOOTH_M,
) -> ElevationProfile:
    """Summarize a route's vertical profile (scope 9)."""
    cleaned = strip_spikes(elevations)
    known = [e for e in cleaned if e is not None]
    if not known:
        return ElevationProfile(samples_missing=len(cleaned))

    gain, loss = _accumulate(cleaned, gain_threshold_m)
    grade_list = grades(route, cleaned, smooth_m=smooth_m)
    climb, descent = _longest_runs(route, cleaned, grade_list)

    return ElevationProfile(
        gain_m=gain,
        loss_m=loss,
        min_ele_m=min(known),
        max_ele_m=max(known),
        grade_histogram=_histogram(route, grade_list),
        longest_climb=climb,
        longest_descent=descent,
        samples_missing=sum(1 for e in cleaned if e is None),
    )


def _accumulate(elevations: list[float | None], threshold_m: float) -> tuple[float, float]:
    """Gain and loss, ignoring wobble below `threshold_m`.

    Accumulating every sample-to-sample difference turns DEM noise into hundreds of
    metres of phantom climbing over a long route, which is why a threshold is standard.
    """
    gain = loss = 0.0
    anchor: float | None = None
    for value in elevations:
        if value is None:
            continue
        if anchor is None:
            anchor = value
            continue
        delta = value - anchor
        if delta >= threshold_m:
            gain += delta
            anchor = value
        elif delta <= -threshold_m:
            loss += -delta
            anchor = value
    return gain, loss


def _histogram(route: Route, grade_list: list[float | None]) -> dict[str, float]:
    """Metres of travel in each grade bin, keyed by the bin's lower bound in percent."""
    bins: dict[str, float] = {}
    for i, grade in enumerate(grade_list):
        if grade is None:
            continue
        span = _span_m(route, i)
        lower = int(grade // GRADE_BIN_PCT) * GRADE_BIN_PCT
        bins[f"{lower:+.0f}"] = bins.get(f"{lower:+.0f}", 0.0) + span
    return dict(sorted(bins.items(), key=lambda kv: float(kv[0])))


def _span_m(route: Route, i: int) -> float:
    """Distance attributable to point `i`: half the gap on each side."""
    points = route.points
    before = points[i].cum_dist_m - points[i - 1].cum_dist_m if i > 0 else 0.0
    after = points[i + 1].cum_dist_m - points[i].cum_dist_m if i < len(points) - 1 else 0.0
    return (before + after) / 2.0


def _longest_runs(
    route: Route, elevations: list[float | None], grade_list: list[float | None]
) -> tuple[SustainedRun | None, SustainedRun | None]:
    """The longest continuous climb and descent, by distance."""
    climb = _longest_run(route, elevations, grade_list, ascending=True)
    descent = _longest_run(route, elevations, grade_list, ascending=False)
    return climb, descent


def _longest_run(
    route: Route,
    elevations: list[float | None],
    grade_list: list[float | None],
    ascending: bool,
) -> SustainedRun | None:
    best: SustainedRun | None = None
    start: int | None = None

    for i, grade in enumerate([*grade_list, None]):
        signed_ok = grade is not None and (grade > 0 if ascending else grade < 0)
        if signed_ok and start is None:
            start = i
        elif not signed_ok and start is not None:
            run = _make_run(route, elevations, start, i - 1)
            if run and (best is None or run.length_m > best.length_m):
                best = run
            start = None
    return best


def _make_run(
    route: Route, elevations: list[float | None], start: int, end: int
) -> SustainedRun | None:
    """Build a run from DEM elevations, never from the GPX's own ele tags."""
    if end <= start:
        return None
    a, b = route.points[start], route.points[end]
    length = b.cum_dist_m - a.cum_dist_m
    ele_a, ele_b = elevations[start], elevations[end]
    if length <= 0 or ele_a is None or ele_b is None:
        return None
    rise = ele_b - ele_a
    return SustainedRun(
        start_m=a.cum_dist_m,
        end_m=b.cum_dist_m,
        gain_m=rise,
        mean_grade_pct=rise / length * 100.0,
    )
