"""Where a preference pair comes from (scope 7.1), and what it refuses to invent.

Both sources scope 7.1 names are empty on this machine, so these tests are about the shape
of the evidence rather than about any runner's actual taste. The load-bearing ones are the
refusals: an edit that changed nothing is not a preference, and an unmatched way is not a
quiet LTS 1.
"""

from __future__ import annotations

import pytest

from longrun.core.models.geometry import Segment
from longrun.core.routing.preferences import (
    pairs_from_edit,
    pairs_from_history,
    summarise,
    way_class,
)
from longrun.core.routing.tuning import RouteSummary, WayClass


def _segment(index: int, way_id: int | None, length_m: float) -> Segment:
    return Segment(
        id=f"s{index:05d}",
        index=index,
        start_idx=index,
        end_idx=index + 1,
        cum_start_m=index * length_m,
        length_m=length_m,
        way_id=way_id,
    )


# --- classifying a way -------------------------------------------------------


def test_a_footway_is_a_path_and_a_secondary_is_a_collector() -> None:
    """The two classes `to_custom_model` matches on. Getting either wrong means fitting a
    parameter against ways the router will never apply it to."""
    assert way_class({"highway": "footway"}).is_path
    assert way_class({"highway": "secondary"}).is_collector
    assert not way_class({"highway": "residential"}).is_collector


def test_an_unmatched_way_is_unknown_rather_than_quietly_level_one() -> None:
    """Scope 12, and it matters more here than usual: charging an unmatched way the LTS 2
    multiplier fits a parameter against missing data and then calls it a preference."""
    assert way_class(None) == WayClass()
    assert way_class(None).lts is None
    assert way_class({}).lts is None


def test_surface_is_read_with_osms_spelling_and_not_the_routers() -> None:
    """`to_custom_model` emits `surface == DIRT`; OSM writes `surface=dirt`. Two
    vocabularies for one idea, and the translation has to happen somewhere visible."""
    assert way_class({"highway": "path", "surface": "gravel"}).unpaved
    assert not way_class({"highway": "path", "surface": "asphalt"}).unpaved


# --- summarising a route -----------------------------------------------------


def test_a_route_is_summarised_by_metres_per_class() -> None:
    """Which is all a cost needs, and is what makes a labelled set self-contained YAML
    rather than a directory of fixtures."""
    segments = [_segment(0, 1, 100.0), _segment(1, 2, 200.0), _segment(2, 1, 50.0)]
    tags = {1: {"highway": "footway"}, 2: {"highway": "secondary"}}

    summary = summarise("r", segments, tags)

    assert summary.length_m == pytest.approx(350.0)
    assert summary.metres_by_class[WayClass(lts=1, is_path=True).key()] == pytest.approx(150.0)


def test_segments_on_ways_with_no_tags_still_count_toward_length() -> None:
    """A route is its whole length whether or not the ways under it were matched. Dropping
    the unmatched part would make a detour ratio computed from this quietly wrong."""
    summary = summarise(
        "r", [_segment(0, None, 400.0), _segment(1, 7, 100.0)], {7: {"highway": "footway"}}
    )

    assert summary.length_m == pytest.approx(500.0)
    assert summary.metres_by_class[WayClass().key()] == pytest.approx(400.0)


def test_a_way_id_whose_tags_were_never_loaded_is_also_unknown() -> None:
    """Two different ways of knowing nothing about a way - unmatched, and matched to a way
    whose tags the store could not supply - and both are `unknown`, not level 1."""
    summary = summarise("r", [_segment(0, None, 400.0), _segment(1, 7, 100.0)], {})

    assert summary.metres_by_class[WayClass().key()] == pytest.approx(500.0)


# --- an edit -----------------------------------------------------------------


def _summary(name: str, *ways: tuple[WayClass, float]) -> RouteSummary:
    return RouteSummary.from_ways(name, ways)


def test_an_edited_route_is_the_preferred_one() -> None:
    """Somebody spent effort making it. That is a stronger signal than any questionnaire
    and it is the revealed preference scope 6.3 argues for when it refuses to ask."""
    original = _summary("original", (WayClass(lts=3), 1000.0))
    edited = _summary("edited", (WayClass(lts=1, is_path=True), 1100.0))

    pairs = pairs_from_edit(original, edited)

    assert len(pairs) == 1
    assert pairs[0].chosen is edited
    assert pairs[0].source == "edit"


def test_an_edit_that_changed_nothing_is_not_a_preference() -> None:
    """A pair recording a choice nobody made is noise the fit would count as evidence."""
    same = _summary("a", (WayClass(lts=2), 1000.0))

    assert pairs_from_edit(same, _summary("b", (WayClass(lts=2), 1000.0))) == []


# --- history -----------------------------------------------------------------


def test_a_route_on_roads_the_runner_actually_runs_is_the_preferred_one() -> None:
    """Scope 6.2's accepted-road set, finally consumed by something. ADR 0001 keyed it on
    real `osm_way_id`s and struck the geometry-hashing fallback; this is what that was for.
    """
    familiar = _summary("familiar", (WayClass(lts=1), 1000.0))
    strange = _summary("strange", (WayClass(lts=1), 1000.0))

    pairs = pairs_from_history(
        [(familiar, frozenset({1, 2, 3, 4})), (strange, frozenset({7, 8, 9, 10}))],
        accepted=frozenset({1, 2, 3, 4}),
    )

    assert len(pairs) == 1
    assert pairs[0].chosen is familiar
    assert pairs[0].source == "history"
    assert pairs[0].weight == pytest.approx(1.0)


def test_two_routes_the_runner_knows_equally_well_say_nothing() -> None:
    """And are not recorded. A pair with no difference in it would dilute the evidence
    while looking like more of it."""
    a = _summary("a", (WayClass(lts=1), 1000.0))
    b = _summary("b", (WayClass(lts=2), 1000.0))

    pairs = pairs_from_history(
        [(a, frozenset({1, 2})), (b, frozenset({1, 2}))], accepted=frozenset({1, 2})
    )

    assert pairs == []


def test_a_pair_is_weighted_by_how_clear_cut_it_is() -> None:
    """A route the runner half knows against one they do not is weaker evidence than one
    they know entirely against one they do not, and the fit should feel the difference."""
    a = _summary("a", (WayClass(lts=1), 1000.0))
    b = _summary("b", (WayClass(lts=1), 1000.0))

    clear = pairs_from_history(
        [(a, frozenset({1, 2, 3, 4})), (b, frozenset({9}))], accepted=frozenset({1, 2, 3, 4})
    )
    murky = pairs_from_history(
        [(a, frozenset({1, 2, 3, 4})), (b, frozenset({9}))], accepted=frozenset({1, 2})
    )

    assert clear[0].weight > murky[0].weight


def test_a_route_matched_to_no_ways_at_all_scores_zero_rather_than_dividing_by_it() -> None:
    a = _summary("a", (WayClass(lts=1), 1000.0))
    b = _summary("b", (WayClass(lts=1), 1000.0))

    pairs = pairs_from_history(
        [(a, frozenset({1, 2})), (b, frozenset())], accepted=frozenset({1, 2})
    )

    assert pairs and pairs[0].chosen is a
