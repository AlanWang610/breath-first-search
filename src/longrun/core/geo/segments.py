"""Splitting a route into scoring units (scope 3.4, 8.2).

Segmentation policy, fixed once because everything downstream keys off it:

* **Split where the OSM way changes.** Hostility, legality and surface are way
  attributes, so a segment spanning two ways would have to report a blend of two
  different answers.
* **Subdivide so no segment exceeds `max_len_m`.** Position weighting (scope 8.2) rises
  with distance along the route; on a 4 km uniform stretch a single segment would get one
  weight for terrain that spans a large slice of the run.
* **Segment ids are stable across reruns of the same route.** A golden `expected.json`
  diff is only reviewable if ids do not shift when an unrelated scorer changes.

Segments partition the route exactly: no gaps, no overlaps, first starts at point 0 and
last ends at the final point.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from longrun.core.geo.projections import bbox_of
from longrun.core.models.geometry import Corridor, Route, Segment
from longrun.core.models.request import LockedRange

#: Long enough to keep segment counts sane on a 100 km route, short enough that position
#: weighting stays meaningful. Roughly a city block to a few blocks.
DEFAULT_MAX_SEGMENT_M = 250.0

#: Scope 5: scorers query a 300-500 m buffer around the route.
DEFAULT_CORRIDOR_BUFFER_M = 400.0

#: How far two segment boundaries may lie apart and still be called the same place.
#:
#: One metre, and the number is not doing much work: an edit that leaves a stretch alone
#: leaves its *points* alone, so the unchanged part of a spliced or rerouted line agrees
#: with the stored one to float noise rather than to a metre. The tolerance exists because
#: `cum_dist_m` is re-accumulated from the first point on every pass, and because a line
#: that came back from the router twice can differ in its seventh decimal place. It is far
#: below `DEFAULT_MAX_SEGMENT_M`, which is what stops it reaching a neighbour.
SAME_GROUND_M = 1.0


def segment_id(index: int) -> str:
    """Stable, sortable identity for a segment of a given route."""
    return f"s{index:05d}"


def segment_route(
    route: Route,
    way_ids: list[int | None] | None = None,
    max_len_m: float = DEFAULT_MAX_SEGMENT_M,
) -> list[Segment]:
    """Split a route into scoring segments.

    `way_ids` is aligned to `route.points` and gives the OSM way each point lies on; pass
    None when the route has not been matched to ways yet, and splitting falls back to
    length alone.
    """
    if max_len_m <= 0:
        raise ValueError("max_len_m must be positive")
    if way_ids is not None and len(way_ids) != len(route.points):
        raise ValueError(f"way_ids has {len(way_ids)} entries for {len(route.points)} route points")

    boundaries = _boundary_indices(route, way_ids, max_len_m)

    segments: list[Segment] = []
    for index, (start_idx, end_idx) in enumerate(zip(boundaries[:-1], boundaries[1:], strict=True)):
        cum_start = route.points[start_idx].cum_dist_m
        segments.append(
            Segment(
                id=segment_id(index),
                index=index,
                start_idx=start_idx,
                end_idx=end_idx,
                cum_start_m=cum_start,
                length_m=route.points[end_idx].cum_dist_m - cum_start,
                way_id=way_ids[start_idx] if way_ids else None,
            )
        )
    return segments


def _boundary_indices(
    route: Route, way_ids: list[int | None] | None, max_len_m: float
) -> list[int]:
    """Point indices where one segment ends and the next begins, including both ends.

    A length split closes the segment at the last point that still fits, rather than at
    the first point that overruns, so `max_len_m` is an upper bound and not a target. The
    single exception is a route whose consecutive points are themselves further apart
    than `max_len_m`: that step cannot be subdivided without inventing geometry.
    """
    boundaries = [0]
    run_start_m = route.points[0].cum_dist_m

    for i in range(1, len(route.points)):
        way_changed = way_ids is not None and way_ids[i] != way_ids[i - 1]
        overruns = route.points[i].cum_dist_m - run_start_m > max_len_m

        if way_changed:
            cut = i
        elif overruns:
            # Close before the overrun; fall back to i when the single step is too long.
            cut = i - 1 if i - 1 > boundaries[-1] else i
        else:
            continue

        if cut != boundaries[-1]:
            boundaries.append(cut)
            run_start_m = route.points[cut].cum_dist_m

    last = len(route.points) - 1
    if boundaries[-1] != last:
        boundaries.append(last)
    return boundaries


def corridor(
    route: Route, buffer_m: float = DEFAULT_CORRIDOR_BUFFER_M, pad_deg: float = 0.01
) -> Corridor:
    """The query window every scorer opens with (scope 5)."""
    return Corridor(route_id=route.id, buffer_m=buffer_m, bbox=bbox_of(route, pad_deg))


def corridor_polygon(corridor: Corridor) -> Any:
    """The corridor as a WGS84 polygon, for a store to query with.

    Defined once, here, because every `LayerStore` implementation must query the *same*
    geometry: if the file store and PostGIS each built their own, an equivalence test
    between them would be comparing two different questions and could not tell a query
    bug from a geometry difference.

    Buffered in the corridor's local metric CRS and transformed back, never buffered in
    degrees - 400 m of longitude is not 400 m of latitude, and the error grows with
    latitude until a corridor in Alaska is several times wider than one in Texas.
    """
    from pyproj import CRS
    from shapely.geometry import box

    from longrun.core.geo.projections import transformer_from, transformer_to, utm_epsg

    centre_lat = (corridor.bbox.min_lat + corridor.bbox.max_lat) / 2
    centre_lon = (corridor.bbox.min_lon + corridor.bbox.max_lon) / 2
    crs = CRS.from_epsg(utm_epsg(centre_lat, centre_lon))

    rect = box(
        corridor.bbox.min_lon,
        corridor.bbox.min_lat,
        corridor.bbox.max_lon,
        corridor.bbox.max_lat,
    )
    to_local, to_wgs = transformer_to(crs), transformer_from(crs)
    xs, ys = to_local.transform(*rect.exterior.coords.xy)
    local = box(min(xs), min(ys), max(xs), max(ys)).buffer(corridor.buffer_m)
    bx, by = to_wgs.transform(*local.exterior.coords.xy)
    return box(min(bx), min(by), max(bx), max(by))


def position_fraction(segment: Segment, total_m: float) -> float:
    """Where a segment sits along the route, 0 at the start and 1 at the finish.

    Measured at the segment midpoint: using the start would under-weight a long final
    segment, and using the end would over-weight the first one.
    """
    if total_m <= 0:
        return 0.0
    midpoint = segment.cum_start_m + segment.length_m / 2.0
    return min(max(midpoint / total_m, 0.0), 1.0)


def locked_segment_ids(segments: list[Segment], locked: list[LockedRange]) -> frozenset[str]:
    """Segments overlapping any locked range (scope 6.4).

    Overlap, not containment: a lock that covers half a segment still means the loop may
    not reroute through it, so the whole segment is excluded from the worst-N candidates.
    """
    ids: set[str] = set()
    for segment in segments:
        for lock in locked:
            if segment.cum_start_m < lock.end_m and segment.cum_end_m > lock.start_m:
                ids.add(segment.id)
                break
    return frozenset(ids)


@dataclass(frozen=True)
class SegmentMap:
    """Which ids of one segmentation still name the same ground in another (ADR 0032).

    `segment_id(index)` is positional - `f"s{index:05d}"` - so an edit that inserts or
    removes a single point renumbers every segment after it. Anything holding an id from
    before the edit is then holding an index into a list that no longer exists, and the
    dangerous part is that the id is still *valid*: `s00042` names a segment either way, a
    few hundred metres from the one it was measured on.

    So this is the inverse index, in the same spirit as M10.12's
    `cumulative_after_normalize`: a way to ask "where did this go" that goes through the
    ground rather than through the number.
    """

    #: False when the two segmentations describe the same line cut the same way. Then
    #: `onto` is the identity and nothing was lost, and a caller can carry unchanged.
    moved: bool
    #: Old id -> new id, for the segments whose ground survives at the same distance.
    onto: Mapping[str, str] = field(default_factory=dict)
    #: Old ids with no counterpart. Reported rather than swallowed: "this measurement is
    #: about ground that is not on the route any more" is an answer, and dropping it
    #: silently is how a plan sheet ends up describing a street the runner will not see.
    lost: tuple[str, ...] = ()
    #: `(cum_start_m, cum_end_m)` for each stretch that survived, in route order.
    #:
    #: Here because not everything a scorer records is keyed on a segment: a `PlanWaypoint`
    #: is keyed on `cum_dist_m`, and the question for one of those is whether the ground at
    #: that distance is ground this map matched. Taken from the *new* segmentation, which is
    #: the one whose distances this pass believes.
    kept_spans: tuple[tuple[float, float], ...] = ()

    def __contains__(self, old_id: str) -> bool:
        """Whether `old_id` named a segment of the segmentation this map came from.

        The third case a caller has to tell apart. An id that is neither kept nor lost was
        never a segment id: `ROUTE_SUMMARY_ID`, a `start@07:00` sweep row, a `meet#0` crew
        point. Those describe the whole route rather than a piece of it.
        """
        return old_id in self.onto or old_id in self.lost


def map_segments(
    old_route: Route,
    old: Sequence[Segment],
    new_route: Route,
    new: Sequence[Segment],
    tolerance_m: float = SAME_GROUND_M,
) -> SegmentMap:
    """Where each of `old`'s segments went in `new`, by ground rather than by index.

    **Matched on `cum_start_m` and `length_m`, then checked against the line.** The span is
    the cheap half and it is what makes the search local; on its own it is not enough, and
    the counter-example is not exotic. Replace a kilometre in the middle of a route with a
    different kilometre of the same length and every distance downstream is unchanged, so a
    span match alone would re-point every measurement in the replaced stretch at new ground
    with no sign that anything happened. The endpoints of the candidate are therefore
    compared against the endpoints of the original, on their own routes, and a segment whose
    span is right but whose ground is not counts as lost.

    Only `lat`/`lon` are compared, never `ele_m`: `_score_pass` writes terrain heights onto
    the points it returns, so the elevations of two readings of the same line differ
    whenever one of them ran on a machine with a DEM and the other did not. That is a
    difference in what was measured, not in where the segment is.

    **A segment that moved by less than a metre still counts as the same place.** There is
    no attempt to track a segment that *slid* - a stretch pushed 300 m down the route by an
    insertion upstream is reported lost, not followed. Following it would need a claim about
    which edit happened, and an edit is not always a single contiguous splice; the honest
    answer for a measurement whose distance-along-route changed is that the thing it
    measured has to be measured again.
    """
    if _same_line(old_route, old, new_route, new):
        return SegmentMap(
            moved=False,
            onto={segment.id: segment.id for segment in old},
            kept_spans=tuple((s.cum_start_m, s.cum_end_m) for s in new),
        )

    by_metre: dict[int, list[Segment]] = {}
    for segment in new:
        by_metre.setdefault(int(segment.cum_start_m), []).append(segment)

    onto: dict[str, str] = {}
    lost: list[str] = []
    spans: list[tuple[float, float]] = []
    for segment in old:
        anchor = int(segment.cum_start_m)
        candidates = [
            candidate
            for metre in (anchor - 1, anchor, anchor + 1)
            for candidate in by_metre.get(metre, ())
            if abs(candidate.cum_start_m - segment.cum_start_m) <= tolerance_m
            and abs(candidate.length_m - segment.length_m) <= tolerance_m
            and _ends_agree(old_route, segment, new_route, candidate, tolerance_m)
        ]
        if len(candidates) == 1:
            onto[segment.id] = candidates[0].id
            spans.append((candidates[0].cum_start_m, candidates[0].cum_end_m))
        else:
            lost.append(segment.id)
    return SegmentMap(moved=True, onto=onto, lost=tuple(lost), kept_spans=tuple(sorted(spans)))


def _same_line(
    old_route: Route, old: Sequence[Segment], new_route: Route, new: Sequence[Segment]
) -> bool:
    """Whether both segmentations cut the same line in the same places.

    The fast path, and the one that keeps `longrun refresh` exactly what it was: a refresh
    passes no router, so the line is identical by construction (ADR 0030) and every carried
    result keeps every measurement it had.
    """
    if len(old) != len(new) or list(old) != list(new):
        return False
    return [(p.lat, p.lon) for p in old_route.points] == [(p.lat, p.lon) for p in new_route.points]


def _ends_agree(
    old_route: Route, old: Segment, new_route: Route, new: Segment, tolerance_m: float
) -> bool:
    """Whether two segments start and finish at the same places on the ground."""
    from longrun.core.geo.gpx import haversine_m

    for old_idx, new_idx in ((old.start_idx, new.start_idx), (old.end_idx, new.end_idx)):
        if old_idx >= len(old_route.points) or new_idx >= len(new_route.points):
            return False
        here, there = old_route.points[old_idx], new_route.points[new_idx]
        if haversine_m(here.lat, here.lon, there.lat, there.lon) > tolerance_m:
            return False
    return True
