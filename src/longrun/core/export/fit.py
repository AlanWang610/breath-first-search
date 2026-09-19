"""A FIT course file, written by hand (scope 9, 10.1's `longrun export --fit`).

**No new dependency, deliberately.** A FIT *course* is a closed subset of the format - one
`file_id`, one `course`, one `lap`, N `record`, M `course_point`, a 14-byte header and two
CRCs - which is `struct.pack` and a 16-entry CRC table. Four arguments against taking a
library for it:

* `fit-tool`, the only Python package that writes FIT, is a stale 0.x with known bugs. This
  project has been burned twice by pinned upstreams already: `mcp>=1.2` admitted a breaking
  2.x, and fitdecode 0.11.0 needed a `FitReader` subclass to work around a crash.
* Garmin's own Python SDK **cannot create** FIT files at all, only read them - so the
  official route does not exist.
* The authoritative constants are already installed: `fitdecode` is in the `history` extra
  and CI syncs it, and it ships the FIT profile. Every number below was read out of
  `fitdecode.profile` rather than transcribed from a PDF, and a test asserts they still
  agree - including `FIT_EPOCH`, which is the one most likely to be wrong.
* `core/` stays importable on a bare `uv sync` **by construction**, with no optional import
  to guard and no `--extra export` for CI to forget.

The round trip is what makes this safe rather than brave: `fitdecode` validates the CRC on
read, so a test that decodes what this wrote is a real check of the encoder.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from datetime import UTC, datetime
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

#: Seconds between the Unix epoch and the FIT epoch, 1989-12-31T00:00:00Z. Equal to
#: `fitdecode.FIT_UTC_REFERENCE`, and a test asserts that rather than trusting this line.
FIT_EPOCH = 631_065_600

#: Degrees to semicircles, which is how FIT stores a coordinate.
_SEMICIRCLES = 2**31 / 180.0

#: `course_point.type`, read from `fitdecode.profile.FIELD_TYPES["course_point"].enum`.
#: All eight of our kinds have a native member, so **nothing degrades in FIT** - the
#: generic fallback is a TCX rule only.
COURSE_POINT_TYPE: dict[WaypointKind, int] = {
    "water": 3,  # water
    "food": 4,  # food
    "hazard": 5,  # danger
    "marker": 34,  # mile_marker
    "crew": 37,  # meeting_spot
    "toilet": 39,  # toilet
    "gate": 46,  # obstacle
    "bailout": 51,  # transport
}

#: FIT's own CRC-16, four bits at a time.
_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip

# Base types, from the FIT profile.
_ENUM, _UINT8, _SINT32, _UINT32, _STRING, _UINT16 = 0x00, 0x02, 0x85, 0x86, 0x07, 0x84

_NAME_BYTES = 16


def crc16(data: bytes, crc: int = 0) -> int:
    """FIT's CRC-16. Cross-checked against `fitdecode.utils.compute_crc` in the tests."""
    for byte in data:
        for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
            table = _CRC_TABLE[crc & 0x0F]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ table ^ _CRC_TABLE[nibble]
    return crc


def _timestamp(when: datetime) -> int:
    moment = when if when.tzinfo else when.replace(tzinfo=UTC)
    return max(0, int(moment.timestamp()) - FIT_EPOCH)


def _semicircles(degrees: float) -> int:
    return int(degrees * _SEMICIRCLES)


def _definition(local: int, global_num: int, fields: Sequence[tuple[int, int, int]]) -> bytes:
    body = struct.pack("<BBHB", 0, 0, global_num, len(fields))
    for number, size, base in fields:
        body += struct.pack("<BBB", number, size, base)
    return bytes([0x40 | local]) + body


def _data(local: int, payload: bytes) -> bytes:
    return bytes([local]) + payload


def fit_course(
    route: Route,
    waypoints: Sequence[PlanWaypoint],
    *,
    etas: Sequence[datetime],
    name: str | None = None,
    max_course_points: int = DEVICE_COURSE_POINT_MAX,
) -> tuple[bytes, list[str]]:
    """The FIT course as bytes, and the notes saying what the device cap dropped."""
    require_etas(route, etas)
    kept, notes = select_for_device(waypoints, route.length_m, max_course_points)
    title = (route.name or name or route.id).encode("utf-8")[: _NAME_BYTES - 1]

    body = bytearray()

    # file_id: this is a course, not an activity. A reader that got this wrong would try to
    # import a planned route as a completed run.
    body += _definition(0, 0, [(0, 1, _ENUM), (1, 2, _UINT16), (4, 4, _UINT32)])
    body += _data(0, struct.pack("<BHI", 6, 1, _timestamp(etas[0])))  # 6 = course

    body += _definition(1, 31, [(5, _NAME_BYTES, _STRING), (4, 1, _ENUM)])
    body += _data(1, title.ljust(_NAME_BYTES, b"\x00") + bytes([1]))  # 1 = running

    body += _definition(
        2,
        19,
        [
            (253, 4, _UINT32),
            (2, 4, _UINT32),
            (7, 4, _UINT32),
            (3, 4, _SINT32),
            (4, 4, _SINT32),
            (5, 4, _SINT32),
            (6, 4, _SINT32),
            (9, 4, _UINT32),
        ],
    )
    first, last = route.points[0], route.points[-1]
    body += _data(
        2,
        struct.pack(
            "<IIIiiiiI",
            _timestamp(etas[-1]),
            _timestamp(etas[0]),
            int((etas[-1] - etas[0]).total_seconds() * 1000),
            _semicircles(first.lat),
            _semicircles(first.lon),
            _semicircles(last.lat),
            _semicircles(last.lon),
            int(route.length_m * 100),
        ),
    )

    body += _definition(
        3,
        20,
        [(253, 4, _UINT32), (0, 4, _SINT32), (1, 4, _SINT32), (5, 4, _UINT32), (2, 2, _UINT16)],
    )
    for point, when in zip(route.points, etas, strict=True):
        # Altitude is stored as (metres + 500) * 5. 0xFFFF is the profile's invalid value,
        # which is how "the DEM had nothing here" survives into the file rather than
        # becoming a plausible sea-level reading.
        altitude = 0xFFFF if point.ele_m is None else int((point.ele_m + 500) * 5)
        body += _data(
            3,
            struct.pack(
                "<IiiIH",
                _timestamp(when),
                _semicircles(point.lat),
                _semicircles(point.lon),
                int(point.cum_dist_m * 100),
                min(max(altitude, 0), 0xFFFF),
            ),
        )

    body += _definition(
        4,
        32,
        [
            (254, 2, _UINT16),
            (1, 4, _UINT32),
            (2, 4, _SINT32),
            (3, 4, _SINT32),
            (4, 4, _UINT32),
            (5, 1, _ENUM),
            (6, _NAME_BYTES, _STRING),
        ],
    )
    seen: dict[WaypointKind, int] = {}
    for index, waypoint in enumerate(kept):
        # `at_distance`, never `waypoint.position`: a course point goes on the track. A
        # bailout 6 km off the line would otherwise send a watch 6 km off the line.
        position, arrival, cum_m = at_distance(route, etas, waypoint.cum_dist_m)
        seen[waypoint.kind] = seen.get(waypoint.kind, 0) + 1
        label = short_name(waypoint.kind, seen[waypoint.kind], _NAME_BYTES - 1)
        # Field order here must match the definition above, not the profile's field numbers.
        body += _data(
            4,
            struct.pack(
                "<HIiiIB",
                index,
                _timestamp(arrival or waypoint.eta or etas[0]),
                _semicircles(position.lat),
                _semicircles(position.lon),
                int(cum_m * 100),
                COURSE_POINT_TYPE.get(waypoint.kind, 0),
            )
            + label.encode("utf-8").ljust(_NAME_BYTES, b"\x00"),
        )

    # The 14-byte header carries its own CRC, and the file carries a second over header
    # plus data. `fitdecode` checks both on read, which is what makes the round-trip test
    # a real check of this encoder rather than a check that it produced some bytes.
    header = struct.pack("<BBHI4s", 14, 0x20, 2100, len(body), b".FIT")
    header += struct.pack("<H", crc16(header))
    file_bytes = header + bytes(body)
    return file_bytes + struct.pack("<H", crc16(file_bytes)), notes


def fit_write(
    route: Route,
    waypoints: Sequence[PlanWaypoint],
    destination: str | Path,
    *,
    etas: Sequence[datetime],
    name: str | None = None,
    max_course_points: int = DEVICE_COURSE_POINT_MAX,
) -> tuple[Path, list[str]]:
    data, notes = fit_course(
        route, waypoints, etas=etas, name=name, max_course_points=max_course_points
    )
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, notes


__all__ = ["COURSE_POINT_TYPE", "FIT_EPOCH", "crc16", "fit_course", "fit_write"]
