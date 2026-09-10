"""Plumbing every scorer in this package shares (scope 4.1, 7.2).

Extracted from `hostility.py`, which carried it while this package had six modules and
said so: *"they are the first thing that should move when a `core/scorers/_common.py` is
allowed to exist."* The scope 7.4 environment scorers are the seventh through twelfth, so
it now exists.

Nothing here decides anything. It is the two joins every scorer needs before it can
measure — OSM tags for the way a segment lies on, and the route in a metric CRS so a point
feature can be placed at a kilometre mark — plus the vocabulary they agree on for "the
whole route" and "we could not tell".

Private by name because it is an internal contract between the scorers, not an API: a
caller outside this package that needs a route frame wants `core.geo`, not this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shapely.geometry import LineString, Point

from longrun.core.geo.projections import local_crs, transformer_to
from longrun.core.geo.segments import DEFAULT_CORRIDOR_BUFFER_M, corridor
from longrun.core.models.geometry import Route, Segment

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext


#: The layer every tag-reading scorer opens.
WAYS_LAYER = "ways"

#: Column carrying the OSM way id in a `ways` frame; the join key to `Segment.way_id`.
WAY_ID_COLUMN = "way_id"

#: Columns an HPMS conflation may have written onto the ways frame (scope 5, 7.2). AADT
#: is a confidence-raiser for `lts_from_tags`, never a requirement.
AADT_COLUMNS = ("aadt", "AADT", "aadt_veh_day")

#: `SegmentMeasurement.segment_id` for the one row describing the whole route. Route
#: totals ("fraction of length at LTS >= 3", scope 7.1) have no segment to live on, and
#: real segment ids are `s00000`-shaped, so this cannot collide with one.
ROUTE_SUMMARY_ID = "route"

#: Confidence for a segment whose way is not in the layer at all. Not zero: we still know
#: where the segment is, only nothing about what it is made of.
UNKNOWN_WAY_CONFIDENCE = 0.3


def row_tags(row: Any) -> dict[str, Any]:
    """One GeoDataFrame row as an OSM tag dict, with geometry and nulls dropped.

    Nulls are dropped rather than carried as None so that `tags.get("sidewalk")` means
    "this way has no sidewalk tag" — which `has_sidewalk` reads as *unknown* — instead of
    a NaN that a later truthiness test would read as a value (scope 12).
    """
    tags: dict[str, Any] = {}
    for key, value in row.items():
        if key == "geometry" or value is None:
            continue
        try:
            if bool(value != value):  # NaN and pandas NA are not equal to themselves
                continue
        except (TypeError, ValueError):  # pragma: no cover - exotic array-valued cell
            pass
        tags[key] = value
    return tags


def frame_tags_by_way(frame: Any) -> dict[int, dict[str, Any]]:
    """Index an already-fetched ways frame by way id."""
    by_way: dict[int, dict[str, Any]] = {}
    if len(frame) == 0 or WAY_ID_COLUMN not in frame.columns:
        return by_way
    for _, row in frame.iterrows():
        try:
            way_id = int(row[WAY_ID_COLUMN])
        except (TypeError, ValueError):
            continue
        by_way[way_id] = row_tags(row)
    return by_way


def way_tags_in_corridor(
    route: Route, ctx: ScorerContext, buffer_m: float = DEFAULT_CORRIDOR_BUFFER_M
) -> dict[int, dict[str, Any]]:
    """Tags of every way in the route corridor, keyed by way id.

    Raises `LayerNotFound` when the layer is absent; callers turn that into an
    `unavailable()` result rather than letting it escape (scope 3.6).
    """
    return frame_tags_by_way(ctx.layers.ways_in_corridor(corridor(route, buffer_m=buffer_m)))


def segment_tags(segment: Segment, by_way: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """The tags of the way a segment lies on, or None when they are not known.

    None is a third answer, distinct from an empty tag dict: an unmatched segment has told
    us nothing, and every caller lowers confidence rather than assuming defaults.
    """
    if segment.way_id is None:
        return None
    return by_way.get(int(segment.way_id))


def aadt_of(tags: dict[str, Any]) -> float | None:
    """Annual average daily traffic from a conflated column, if the frame carries one."""
    for column in AADT_COLUMNS:
        if column not in tags:
            continue
        try:
            return float(tags[column])
        except (TypeError, ValueError):
            return None
    return None


class RouteFrame:
    """The route in its own metric CRS: distance *along* it, and distance *to* it.

    Both queries are needed by half the scorers here — a crossing has to be placed at a
    kilometre mark, an amenity has to be shown to be beside the route rather than merely
    inside its bounding box — and neither may be computed in degrees (scope 4.1).

    Distances along are rescaled onto the route's own `cum_dist_m`, which is haversine, so
    a located point lands in the same coordinate the segments are indexed by.
    """

    def __init__(self, route: Route) -> None:
        to_local = transformer_to(local_crs(route))
        xs, ys = to_local.transform([p.lon for p in route.points], [p.lat for p in route.points])
        self._to_local = to_local
        self.line = LineString(list(zip(xs, ys, strict=True)))
        self.length_m = route.length_m
        self._scale = self.length_m / self.line.length if self.line.length > 0 else 1.0

    def locate(self, lon: float, lat: float) -> tuple[float, float]:
        """`(cum_dist_m of the nearest point on the route, perpendicular distance in m)`."""
        x, y = self._to_local.transform(lon, lat)
        point = Point(x, y)
        return self.line.project(point) * self._scale, self.line.distance(point)


def segment_at(segments: list[Segment], cum_m: float) -> Segment | None:
    """The segment containing a distance-along-route, or None when it falls outside."""
    if not segments:
        return None
    for segment in segments:
        if cum_m < segment.cum_end_m:
            return segment
    last = segments[-1]
    return last if cum_m <= last.cum_end_m + 1.0 else None


__all__ = [
    "AADT_COLUMNS",
    "ROUTE_SUMMARY_ID",
    "UNKNOWN_WAY_CONFIDENCE",
    "WAYS_LAYER",
    "WAY_ID_COLUMN",
    "RouteFrame",
    "aadt_of",
    "frame_tags_by_way",
    "row_tags",
    "segment_at",
    "segment_tags",
    "way_tags_in_corridor",
]
