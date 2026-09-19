"""A TCX course (scope 9, 10.1's `longrun export --tcx`).

Stdlib XML and no dependency: TCX is Garmin's XML training format and the schema is small.

**Three traps, all of which fail silently on a device rather than loudly here**, and they
are why TCX is written before FIT: both degradation rules live in this format, so they get
designed against the format that forces them rather than against FIT's richer enum.

1. `CoursePoint/Name` is `Token_t` with `maxLength` **10**. A longer value is an XSD
   violation and Garmin Connect rejects the upload; it does not truncate. `short_name`
   builds `H2O3`, `WC2`, `BAIL12` - short by construction and unique, because a course with
   three points named WATER is legal and unreadable. The human label goes in `<Notes>`,
   which is not capped.
2. `PointType` is a **closed** 16-member enumeration. A kind with no member in it is written
   as `Generic` and keeps its identity in the *name* - which is why `NAME_PREFIX` is keyed on
   kind rather than on the target type. A waypoint is never dropped for want of a type; only
   for the device cap, and then it is reported.
3. `CoursePoint_t` is an XSD **sequence**, so element order is part of validity:
   `Name, Time, Position, AltitudeMeters?, PointType, Notes?`. `Time` and `Position` are
   both required, which is why `plan.etas` is load-bearing for TCX as well as for FIT.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from longrun.core.export.course import (
    DEVICE_COURSE_POINT_MAX,
    at_distance,
    require_etas,
    select_for_device,
    short_name,
)
from longrun.core.models.geometry import Route
from longrun.core.models.waypoint import PlanWaypoint, WaypointKind

_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"

#: `CoursePointType_t`, the whole closed set. Anything not in here is invalid XML, so the
#: mapping below may only produce these values.
TCX_POINT_TYPES = frozenset(
    {
        "Generic",
        "Summit",
        "Valley",
        "Water",
        "Food",
        "Danger",
        "Left",
        "Right",
        "Straight",
        "First Aid",
        "4th Category",
        "3rd Category",
        "2nd Category",
        "1st Category",
        "Hors Category",
        "Sprint",
    }
)

#: Our kinds to that set. Five of the eight have no member and degrade to `Generic`, keeping
#: their identity in the point's name - a reader loses an icon and keeps the fact.
TCX_POINT_TYPE: dict[WaypointKind, str] = {
    "water": "Water",
    "food": "Food",
    "hazard": "Danger",
    "toilet": "Generic",
    "bailout": "Generic",
    "gate": "Generic",
    "crew": "Generic",
    "marker": "Generic",
}

#: `Token_t` maxLength on `CoursePoint/Name`.
TCX_NAME_MAX = 10

#: `Course/Name` is also a `Token_t`, capped at 15.
TCX_COURSE_NAME_MAX = 15


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def tcx_course(
    route: Route,
    waypoints: Sequence[PlanWaypoint],
    *,
    etas: Sequence[datetime],
    name: str | None = None,
    max_course_points: int = DEVICE_COURSE_POINT_MAX,
) -> tuple[str, list[str]]:
    """The TCX document, and the notes saying what degraded or was dropped.

    The notes are scope 3.6 applied to a writer: a format that cannot carry something says
    so, rather than the caller discovering it on a watch.
    """
    require_etas(route, etas)
    kept, notes = select_for_device(waypoints, route.length_m, max_course_points)

    ET.register_namespace("", _NS)
    root = ET.Element(f"{{{_NS}}}TrainingCenterDatabase")
    root.set(f"{{{_XSI}}}schemaLocation", f"{_NS} {_NS}/TrainingCenterDatabasev2.xsd")
    courses = ET.SubElement(root, f"{{{_NS}}}Courses")
    course = ET.SubElement(courses, f"{{{_NS}}}Course")
    ET.SubElement(course, f"{{{_NS}}}Name").text = (route.name or name or route.id)[
        :TCX_COURSE_NAME_MAX
    ]

    lap = ET.SubElement(course, f"{{{_NS}}}Lap")
    ET.SubElement(lap, f"{{{_NS}}}TotalTimeSeconds").text = str(
        round((etas[-1] - etas[0]).total_seconds(), 1)
    )
    ET.SubElement(lap, f"{{{_NS}}}DistanceMeters").text = str(round(route.length_m, 1))
    ET.SubElement(lap, f"{{{_NS}}}Intensity").text = "Active"

    track = ET.SubElement(course, f"{{{_NS}}}Track")
    for point, when in zip(route.points, etas, strict=True):
        trackpoint = ET.SubElement(track, f"{{{_NS}}}Trackpoint")
        ET.SubElement(trackpoint, f"{{{_NS}}}Time").text = _stamp(when)
        where = ET.SubElement(trackpoint, f"{{{_NS}}}Position")
        ET.SubElement(where, f"{{{_NS}}}LatitudeDegrees").text = f"{point.lat:.7f}"
        ET.SubElement(where, f"{{{_NS}}}LongitudeDegrees").text = f"{point.lon:.7f}"
        if point.ele_m is not None:
            ET.SubElement(trackpoint, f"{{{_NS}}}AltitudeMeters").text = f"{point.ele_m:.1f}"
        ET.SubElement(trackpoint, f"{{{_NS}}}DistanceMeters").text = f"{point.cum_dist_m:.1f}"

    degraded: dict[str, int] = {}
    seen: dict[WaypointKind, int] = {}
    for waypoint in kept:
        position, arrival, _ = at_distance(route, etas, waypoint.cum_dist_m)
        seen[waypoint.kind] = seen.get(waypoint.kind, 0) + 1
        point_type = TCX_POINT_TYPE.get(waypoint.kind, "Generic")
        if point_type == "Generic" and waypoint.kind not in ("marker",):
            degraded[waypoint.kind] = degraded.get(waypoint.kind, 0) + 1

        # Element order is the XSD sequence, not a dict's insertion order. A builder that
        # got this wrong would produce a file every validator rejects and most readers
        # accept, which is the worst of both.
        element = ET.SubElement(course, f"{{{_NS}}}CoursePoint")
        ET.SubElement(element, f"{{{_NS}}}Name").text = short_name(
            waypoint.kind, seen[waypoint.kind], TCX_NAME_MAX
        )
        ET.SubElement(element, f"{{{_NS}}}Time").text = _stamp(arrival or waypoint.eta or etas[0])
        at = ET.SubElement(element, f"{{{_NS}}}Position")
        ET.SubElement(at, f"{{{_NS}}}LatitudeDegrees").text = f"{position.lat:.7f}"
        ET.SubElement(at, f"{{{_NS}}}LongitudeDegrees").text = f"{position.lon:.7f}"
        ET.SubElement(element, f"{{{_NS}}}PointType").text = point_type
        note = waypoint.label if not waypoint.detail else f"{waypoint.label} - {waypoint.detail}"
        ET.SubElement(element, f"{{{_NS}}}Notes").text = note

    if degraded:
        detail = ", ".join(f"{count} {kind}" for kind, count in sorted(degraded.items()))
        notes.append(
            f"TCX has no course-point type for {detail}; written as Generic with the kind "
            f"kept in the point's name."
        )

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return body, notes


def tcx_write(
    route: Route,
    waypoints: Sequence[PlanWaypoint],
    destination: str | Path,
    *,
    etas: Sequence[datetime],
    name: str | None = None,
    max_course_points: int = DEVICE_COURSE_POINT_MAX,
) -> tuple[Path, list[str]]:
    body, notes = tcx_course(
        route, waypoints, etas=etas, name=name, max_course_points=max_course_points
    )
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path, notes


__all__ = [
    "TCX_COURSE_NAME_MAX",
    "TCX_NAME_MAX",
    "TCX_POINT_TYPE",
    "TCX_POINT_TYPES",
    "tcx_course",
    "tcx_write",
]
