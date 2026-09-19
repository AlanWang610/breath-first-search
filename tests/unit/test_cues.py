"""Turn instructions, located by distance (scope 7.2).

The fixture is a real GraphHopper 11 answer, captured from the Ozarks graph on 2026-09-18
and committed as JSON - 259 points and 7 instructions over 18.2 km of MO 19. Kept here at
7.5 kB rather than under `tests/golden/routes/`, which is the fixture cap's territory.

`tools/runnability.py` recorded this milestone's trap before the milestone existed: a cue
sheet with no turns in it is indistinguishable from a route that has none. Most of these
tests are about keeping those two apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from longrun.core.geo.gpx import cumulative_after_normalize, densify, normalize
from longrun.core.models.routing import NOT_REQUESTED, NOT_RETURNED, CueSheet
from longrun.core.routing.graphhopper import (
    TURN_SIGNS,
    cue_sheet_of,
    path_to_cues,
    path_to_route,
)

FIXTURE = Path(__file__).parent / "data" / "graphhopper_instructions.json"


def _path() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _raw(path: dict) -> list[tuple[float, float, float | None]]:
    return [(float(c[1]), float(c[0]), None) for c in path["points"]["coordinates"]]


# --- the distance map -------------------------------------------------------------------


def test_a_raw_index_maps_to_the_distance_normalize_gives_that_point() -> None:
    """The invariant the whole cue sheet rests on."""
    raw = _raw(_path())
    cumulative = cumulative_after_normalize(raw)
    assert len(cumulative) == len(raw)
    assert cumulative == sorted(cumulative), "distance along a route does not decrease"

    for point in normalize(raw):
        assert any(abs(point.cum_dist_m - value) < 1e-6 for value in cumulative), (
            "a distance normalize produced does not appear in the index"
        )


def test_densify_leaves_the_kept_points_where_normalize_put_them() -> None:
    """Why no re-location step is needed: a distance from the index is a distance on the
    route `path_to_route` returns, and densify only inserts points *between* kept ones."""
    raw = _raw(_path())
    dense = {round(p.cum_dist_m, 6) for p in densify(normalize(raw))}
    worst = max(min(abs(p.cum_dist_m - d) for d in dense) for p in normalize(raw))
    assert worst < 1e-3, f"densify moved a kept point by {worst} m"


# --- the trap ---------------------------------------------------------------------------


def test_a_cue_lands_where_the_router_put_it_and_not_where_its_index_points() -> None:
    """Measured on this fixture: the arrival instruction indexes raw point 258, which is
    18,153.9 m along. Read as an index into the densified route it is 10,058.8 m - 8.1 km
    early on an 18.2 km route, and a perfectly plausible number."""
    path = _path()
    route = path_to_route(path, route_id="t")
    cues = path_to_cues(path)

    last = cues[-1]
    assert abs(last.cum_dist_m - route.length_m) < 1.0, "the arrival is not at the end"

    naive_index = path["instructions"][-1]["interval"][0]
    naive = route.points[min(naive_index, len(route.points) - 1)].cum_dist_m
    assert abs(naive - last.cum_dist_m) > 5000.0, (
        "the fixture no longer demonstrates the trap, so this test is no longer a test"
    )


def test_every_cue_distance_is_a_distance_on_the_decoded_route() -> None:
    path = _path()
    route = path_to_route(path, route_id="t")
    for cue in path_to_cues(path):
        assert 0.0 <= cue.cum_dist_m <= route.length_m + 1.0


def test_the_cues_are_in_order_along_the_route() -> None:
    distances = [cue.cum_dist_m for cue in path_to_cues(_path())]
    assert distances == sorted(distances)


# --- the four facts ---------------------------------------------------------------------


def test_a_path_with_no_instructions_decodes_to_no_cues_and_says_which() -> None:
    """Every cassette recorded before M10, and the synthetic path `map_match` builds."""
    path = _path()
    del path["instructions"]
    assert path_to_cues(path) == []
    sheet = cue_sheet_of(path)
    assert not sheet.checked
    assert sheet.reason == NOT_REQUESTED


def test_a_route_that_genuinely_has_no_turns_is_not_one_that_was_not_asked() -> None:
    """The distinction `tools/runnability.py` blocked this feature over."""
    path = _path()
    path["instructions"] = []
    assert cue_sheet_of(path).reason == NOT_RETURNED

    turns = cue_sheet_of(_path())
    assert turns.checked and turns.reason is None


def test_an_empty_cue_sheet_is_not_the_same_object_as_an_unproduced_one() -> None:
    assert CueSheet(checked=True).checked
    assert not CueSheet().checked


# --- the vocabulary ---------------------------------------------------------------------


def test_an_unknown_sign_is_reported_as_unknown_and_never_as_straight() -> None:
    """A cue sheet that says "continue" at a fork is worse than one that admits it does not
    know, and 0 is a real code so a `.get(sign, "straight")` would be silently wrong."""
    path = _path()
    path["instructions"] = [
        {"sign": 99, "text": "Do something new", "interval": [0, 1], "distance": 10.0}
    ]
    cue = path_to_cues(path)[0]
    assert cue.manoeuvre == "sign 99"
    assert TURN_SIGNS[0] == "straight"


def test_the_real_fixture_decodes_to_the_turns_a_reader_would_expect() -> None:
    sheet = cue_sheet_of(_path())
    assert sheet.checked
    assert len(sheet.cues) == 7
    assert sheet.turn_count == 6, "the arrival is not a turn"
    assert [c.manoeuvre for c in sheet.cues] == [
        "straight",
        "right",
        "keep left",
        "right",
        "left",
        "left",
        "arrive",
    ]
    assert sheet.cues[1].street_name == "County Road 513"


def test_an_unnamed_way_is_flagged_ambiguous_rather_than_named_empty() -> None:
    """`None` is unknown, not unnamed - scope 12's rule applied to a street name."""
    sheet = cue_sheet_of(_path())
    first = sheet.cues[0]
    assert first.street_name is None
    assert first.ambiguous
    assert "no name" in (first.ambiguity or "")


def test_two_turns_too_close_together_are_flagged() -> None:
    """Both at the same raw point, which is the extreme of the case: a staggered crossing
    where the router emits "turn right then immediately left" and a runner sees one junction.

    The fixture's own points are ~70 m apart, so adjacent indices are *not* close enough -
    which is the right answer, and the reason this plants the pair deliberately.
    """
    path = _path()
    path["instructions"] = [
        {"sign": 2, "text": "Turn right", "street_name": "A", "interval": [10, 10]},
        {"sign": -2, "text": "Turn left", "street_name": "B", "interval": [10, 11]},
        {"sign": 4, "text": "Arrive", "interval": [258, 258]},
    ]
    cues = path_to_cues(path)
    assert cues[0].ambiguous and "to the next turn" in (cues[0].ambiguity or "")
    assert not cues[1].ambiguous, "the second is 70 m from the arrival, which is separable"


def test_a_malformed_interval_does_not_take_the_plan_down() -> None:
    """This decoder runs inside a scoring pass; an IndexError here fails a plan."""
    path = _path()
    path["instructions"] = [
        {"sign": 0, "text": "off the end", "interval": [99999, 99999]},
        {"sign": 0, "text": "negative", "interval": [-5, 0]},
    ]
    cues = path_to_cues(path)
    assert len(cues) == 2
    assert all(cue.cum_dist_m >= 0 for cue in cues)
