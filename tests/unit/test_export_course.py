"""The shared course machinery (scope 9, 10.1).

Tests 1 and 2 are the design, and they only work as a pair: a course point belongs on the
route, a GPX waypoint belongs where the thing actually is. If only one existed, the wrong
one could pass.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta
from io import BytesIO

import pytest

from longrun.core.export.course import (
    DEVICE_COURSE_POINT_MAX,
    NAME_PREFIX,
    ExportError,
    at_distance,
    require_etas,
    select_for_device,
    short_name,
)
from longrun.core.export.fit import COURSE_POINT_TYPE, FIT_EPOCH, crc16, fit_course
from longrun.core.export.tcx import TCX_NAME_MAX, TCX_POINT_TYPES, tcx_course
from longrun.core.geo.gpx import gpx_string
from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.models.waypoint import PlanWaypoint

START = datetime(2026, 3, 15, 7, 0)


def _route(points: int = 101, spacing_m: float = 100.0) -> Route:
    """A straight line east, 10 km at 100 m spacing."""
    return Route(
        id="course",
        points=[
            RoutePoint(lat=37.7749, lon=-122.4194 + i * 0.00114, cum_dist_m=i * spacing_m)
            for i in range(points)
        ],
    )


def _etas(route: Route) -> list[datetime]:
    return [START + timedelta(seconds=30 * i) for i in range(len(route.points))]


def _far_waypoint(route: Route) -> PlanWaypoint:
    """A bailout 6 km north of the line, at 5 km along it - the case the rule exists for."""
    return PlanWaypoint(
        position=LatLon(lat=37.7749 + 0.054, lon=-122.4194 + 50 * 0.00114),
        kind="bailout",
        label="Distant Station",
        cum_dist_m=5000.0,
        scorer="bailouts",
        offset_m=6000.0,
    )


def test_a_course_point_sits_on_the_route_and_not_where_the_thing_is() -> None:
    """A bailout 6 km off the line must not put a course point 6 km off the track."""
    route = _route()
    waypoint = _far_waypoint(route)
    position, arrival, cum_m = at_distance(route, _etas(route), waypoint.cum_dist_m)

    assert cum_m == pytest.approx(5000.0, abs=100.0)
    assert position.lat == pytest.approx(37.7749, abs=1e-6), "the course point left the line"
    assert abs(position.lat - waypoint.position.lat) > 0.05
    assert arrival is not None


def test_the_gpx_keeps_the_true_position() -> None:
    """The other half. The two together are the design; either alone permits the bug."""
    route = _route()
    waypoint = _far_waypoint(route)
    text = gpx_string(route, waypoints=[(waypoint.position, waypoint.label, waypoint.kind)])

    assert f'lat="{waypoint.position.lat}"' in text
    assert "Distant Station" in text


def test_a_course_point_name_is_short_and_unique() -> None:
    """TCX caps `CoursePoint/Name` at ten characters; a longer value fails XSD validation
    and Garmin Connect rejects the upload rather than truncating it."""
    names = [short_name("water", n) for n in range(1, 40)]
    assert all(len(name) <= 10 for name in names)
    assert len(set(names)) == len(names)
    assert short_name("water", 3) == "H2O3"
    assert short_name("toilet", 2) == "WC2"


def test_the_device_cap_thins_by_spacing_rather_than_truncating() -> None:
    """Truncation is the tempting implementation and it produces a course with every café
    in the first 3 km and nothing after - which looks like it worked."""
    route = _route()
    crowded = [
        PlanWaypoint(
            position=LatLon(lat=37.7749, lon=-122.4194 + i * 0.0001),
            kind="food",
            label=f"cafe {i}",
            cum_dist_m=i * 20.0,
            scorer="services_along",
        )
        for i in range(500)
    ]
    kept, notes = select_for_device(crowded, route.length_m)

    assert len(kept) <= DEVICE_COURSE_POINT_MAX
    assert notes and "dropped" in notes[0]
    spread = kept[-1].cum_dist_m - kept[0].cum_dist_m
    total = crowded[-1].cum_dist_m - crowded[0].cum_dist_m
    assert spread > 0.8 * total, "the survivors bunched at one end of the route"


def test_the_cap_never_drops_a_hazard_or_a_gate() -> None:
    """One is a safety finding and the other a locked gate; both matter at the moment you
    reach them, and neither is negotiable against a café."""
    route = _route()
    many = [
        PlanWaypoint(
            position=LatLon(lat=37.7749, lon=-122.4194 + i * 0.0001),
            kind="food",
            label=f"cafe {i}",
            cum_dist_m=i * 20.0,
            scorer="services_along",
        )
        for i in range(400)
    ]
    many.append(
        PlanWaypoint(
            position=LatLon(lat=37.7749, lon=-122.35),
            kind="hazard",
            label="rail_at_grade",
            cum_dist_m=9000.0,
            scorer="hazards",
        )
    )
    kept, _ = select_for_device(many, route.length_m)
    assert any(w.kind == "hazard" for w in kept)


def test_a_short_list_is_kept_whole_and_says_nothing() -> None:
    route = _route()
    few = [
        PlanWaypoint(
            position=LatLon(lat=37.7749, lon=-122.41),
            kind="water",
            label="tap",
            cum_dist_m=1000.0,
            scorer="services_along",
        )
    ]
    kept, notes = select_for_device(few, route.length_m)
    assert kept == few
    assert notes == []


def test_a_plan_with_no_etas_refuses_rather_than_inventing_timestamps() -> None:
    """A FIT record and a TCX CoursePoint both require a time. Fabricating one would put a
    course on a watch claiming a schedule nobody computed."""
    route = _route()
    with pytest.raises(ExportError, match="ETA"):
        require_etas(route, [])
    with pytest.raises(ExportError, match="same line"):
        require_etas(route, _etas(route)[:5])


# --- FIT, checked against the profile fitdecode already ships ----------------------------


def _fit_plan() -> tuple[Route, list[PlanWaypoint], list[datetime]]:
    route = _route()
    etas = _etas(route)
    waypoints = [
        PlanWaypoint(
            position=LatLon(lat=37.7749, lon=-122.41),
            kind=kind,
            label=f"a {kind}",
            cum_dist_m=500.0 * (index + 1),
            scorer="test",
        )
        for index, kind in enumerate(COURSE_POINT_TYPE)
    ]
    return route, waypoints, etas


def test_the_fit_epoch_is_the_one_fitdecode_uses() -> None:
    """The constant most likely to be silently wrong: a course off by 19 years still
    decodes, still validates, and puts every point in 2007."""
    fitdecode = pytest.importorskip("fitdecode")
    assert FIT_EPOCH == fitdecode.FIT_UTC_REFERENCE


def test_our_crc_agrees_with_fitdecodes() -> None:
    """Checked with no file involved, so a CRC bug is distinguishable from a layout bug.

    The empty payload is not compared: `fitdecode.utils.compute_crc` hits a bare `assert 0`
    on `start >= end` rather than returning the seed. Ours returns 0, which is right, and
    the round-trip test is what matters for real files anyway.
    """
    fitdecode = pytest.importorskip("fitdecode")
    assert crc16(b"") == 0
    for payload in (b"\x00", b".FIT", bytes(range(256)), bytes(range(256)) * 7):
        assert crc16(payload) == fitdecode.utils.compute_crc(payload)


def test_every_course_point_type_is_one_fit_defines() -> None:
    """Catches a transposed digit, which would otherwise put a water stop on a summit."""
    fitdecode = pytest.importorskip("fitdecode")
    enum = fitdecode.profile.FIELD_TYPES["course_point"].enum
    assert set(COURSE_POINT_TYPE.values()) <= set(enum)
    assert enum[COURSE_POINT_TYPE["water"]] == "water"
    assert enum[COURSE_POINT_TYPE["hazard"]] == "danger"
    assert enum[COURSE_POINT_TYPE["bailout"]] == "transport"


def test_nothing_degrades_in_fit() -> None:
    """Every WaypointKind has a native member, so the generic fallback is a TCX rule only."""
    assert set(COURSE_POINT_TYPE) == set(NAME_PREFIX)


def test_a_written_course_decodes_with_crc_checking_on() -> None:
    """The round trip that makes a hand-written encoder safe rather than brave."""
    fitdecode = pytest.importorskip("fitdecode")
    route, waypoints, etas = _fit_plan()
    data, _ = fit_course(route, waypoints, etas=etas)

    frames = [
        f
        for f in fitdecode.FitReader(BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE)
        if isinstance(f, fitdecode.FitDataMessage)
    ]
    names = Counter(f.name for f in frames)
    assert names["file_id"] == 1
    assert names["record"] == len(route.points), "a track point was lost"
    assert names["course_point"] == len(waypoints)

    file_id = next(f for f in frames if f.name == "file_id")
    assert file_id.get_value("type") == "course", "written as an activity, not a course"


def test_a_fit_course_point_lands_on_the_route_not_at_the_waypoint() -> None:
    """The positioning rule, asserted against what a device would actually read back."""
    fitdecode = pytest.importorskip("fitdecode")
    route = _route()
    etas = _etas(route)
    far = _far_waypoint(route)
    data, _ = fit_course(route, [far], etas=etas)

    point = next(
        f
        for f in fitdecode.FitReader(BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE)
        if isinstance(f, fitdecode.FitDataMessage) and f.name == "course_point"
    )
    lat = point.get_value("position_lat") * 180.0 / 2**31
    assert lat == pytest.approx(37.7749, abs=1e-4), "the course point left the track"
    assert abs(lat - far.position.lat) > 0.05


# --- TCX, where both degradation rules live ----------------------------------------------


def test_a_tcx_course_point_name_fits_and_is_unique() -> None:
    """Token_t maxLength 10. A longer value is rejected by Garmin Connect, not truncated."""
    route, waypoints, etas = _fit_plan()
    body, _ = tcx_course(route, waypoints * 3, etas=etas)
    names = re.findall(r"<Name>([^<]*)</Name>", body)[1:]  # [0] is the course's own name
    assert names and all(len(n) <= TCX_NAME_MAX for n in names)
    assert len(set(names)) == len(names)


def test_every_tcx_point_type_is_in_the_closed_enumeration() -> None:
    route, waypoints, etas = _fit_plan()
    body, _ = tcx_course(route, waypoints, etas=etas)
    assert set(re.findall(r"<PointType>([^<]*)</PointType>", body)) <= TCX_POINT_TYPES


def test_a_kind_tcx_cannot_carry_degrades_and_keeps_its_name() -> None:
    """The rule, stated once and applied in both writers: a waypoint is never dropped for
    want of a type, only for the cap, and then it is reported."""
    route = _route()
    etas = _etas(route)
    toilet = PlanWaypoint(
        position=LatLon(lat=37.7749, lon=-122.41),
        kind="toilet",
        label="Park toilets",
        cum_dist_m=1000.0,
        scorer="services_along",
    )
    body, notes = tcx_course(route, [toilet], etas=etas)

    assert "<PointType>Generic</PointType>" in body
    assert "<Name>WC1</Name>" in body, "the kind did not survive in the name"
    assert "Park toilets" in body, "the human label was lost"
    assert any("toilet" in note for note in notes), "the degradation was not reported"


def test_a_tcx_course_point_keeps_the_xsd_child_order() -> None:
    """CoursePoint_t is a sequence: Name, Time, Position, AltitudeMeters?, PointType, Notes?.

    A builder that got this wrong produces a file every validator rejects and most readers
    accept, which is the worst of both. Time and Position are required, which is why
    plan.etas is load-bearing for TCX as well as FIT.
    """
    route, waypoints, etas = _fit_plan()
    body, _ = tcx_course(route, waypoints[:1], etas=etas)
    root = ET.fromstring(body)
    ns = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    point = root.find(".//t:CoursePoint", ns)
    assert point is not None
    order = [child.tag.split("}")[1] for child in point]
    assert order == ["Name", "Time", "Position", "PointType", "Notes"]


def test_both_writers_refuse_a_plan_with_no_etas() -> None:
    route, waypoints, _ = _fit_plan()
    with pytest.raises(ExportError):
        fit_course(route, waypoints, etas=[])
    with pytest.raises(ExportError):
        tcx_course(route, waypoints, etas=[])
