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

from typing import Any

from longrun.core.geo.projections import bbox_of
from longrun.core.models.geometry import Corridor, Route, Segment
from longrun.core.models.request import LockedRange

#: Long enough to keep segment counts sane on a 100 km route, short enough that position
#: weighting stays meaningful. Roughly a city block to a few blocks.
DEFAULT_MAX_SEGMENT_M = 250.0

#: Scope 5: scorers query a 300-500 m buffer around the route.
DEFAULT_CORRIDOR_BUFFER_M = 400.0


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
