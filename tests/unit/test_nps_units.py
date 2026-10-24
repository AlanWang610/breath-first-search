"""NPS unit boxes, by park code, committed (M18, ADR 0054).

The committed file is what every NPS request is narrowed by, so it is held to three things
here: that it is what `build_units` makes of its source (deterministic, outward-rounded,
small), that the parks the goldens cross are in it, and that the box test picks the parks a
corridor meets and no others.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from longrun.adapters.portals.nps_units import (
    UNITS,
    build_units,
    load_units,
    units_meeting,
    write_units,
)


def _collection(*features: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"UNIT_CODE": code}, "geometry": geometry}
            for code, geometry in features
        ],
    }


def _square(west: float, south: float, size: float = 0.01) -> list[list[float]]:
    return [
        [west, south],
        [west + size, south],
        [west + size, south + size],
        [west, south + size],
        [west, south],
    ]


def test_a_box_is_rounded_outward_so_it_never_shrinks_inside_its_land() -> None:
    units = build_units(
        _collection(("SAFR", {"type": "Polygon", "coordinates": [_square(-122.4234, 37.8061)]})),
        read_on="2026-10-23",
    )
    [[west, south, east, north]] = units["units"]["safr"]
    assert west <= -122.4234 and south <= 37.8061
    assert east >= -122.4134 and north >= 37.8161
    assert units["read_on"] == "2026-10-23"


def test_a_long_thin_unit_keeps_a_box_per_part_and_merges_the_parts_that_overlap() -> None:
    """The Appalachian Trail's one box would cover half a state; its parts do not."""
    parts = {
        "type": "MultiPolygon",
        "coordinates": [
            [_square(-80.0, 37.0)],
            [_square(-79.995, 37.005)],  # overlaps the first
            [_square(-75.0, 41.0)],
        ],
    }
    boxes = build_units(_collection(("APPA", parts)), read_on="x")["units"]["appa"]
    assert len(boxes) == 2


def test_a_rebuild_from_the_same_download_is_the_same_bytes(tmp_path: Path) -> None:
    collection = _collection(
        ("OZAR", {"type": "Polygon", "coordinates": [_square(-91.4, 37.1)]}),
        ("GOGA", {"type": "Polygon", "coordinates": [_square(-122.5, 37.8)]}),
    )
    write_units(build_units(collection, read_on="2026-10-23"), tmp_path / "a.json")
    write_units(build_units(collection, read_on="2026-10-23"), tmp_path / "b.json")
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


def test_the_committed_file_is_small_sourced_and_covers_the_golden_parks() -> None:
    raw = json.loads(UNITS.read_text(encoding="utf-8"))
    assert UNITS.stat().st_size < 200_000
    assert "public domain" in raw["licence"]
    assert raw["source_url"].startswith("https://")
    units = load_units()
    assert len(units) > 400
    assert all(code == code.lower() and len(code) == 4 for code in units)
    for west, south, east, north in (b for boxes in units.values() for b in boxes):
        assert west <= east and south <= north
    # bay-urban crosses Golden Gate and the Maritime park; ozarks-thin crosses the Ozark
    # National Scenic Riverways.
    assert {"goga", "safr", "ozar"} <= set(units)


def test_a_corridor_names_the_parks_it_meets_and_no_others() -> None:
    assert units_meeting((-91.6, 37.0, -91.2, 37.3)) == ["ozar"]
    assert units_meeting((-122.43, 37.80, -122.40, 37.81)) == ["goga", "safr"]
    assert units_meeting((-100.0, 40.0, -99.99, 40.01)) == []  # central Nebraska
