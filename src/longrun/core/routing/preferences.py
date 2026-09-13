"""Where a preference pair comes from (scope 7.1).

> the ~6 parameters are fit against pairwise route preferences - **the user's manual GPX
> edits (rejected vs. chosen segments) and the accepted-road set from history**

Two sources, and this module is both of them. Neither invents a preference: an edit is
something a runner did to a route this project drew, and the accepted-road set is the roads
their own watch says they run. `tuning.fit` is the consumer and it counts pairs by source,
so a result can never quietly rest on labels nobody supplied.

**A pair is two whole routes, not two segments.** Scope 7.1 says "rejected vs. chosen
segments", and a segment is the right unit for describing what changed - but it is the
wrong unit for costing, because a segment has no cost outside the route it sits in and the
detour-ratio regulariser is defined on whole routes. So `route_diff` finds *where* two
routes disagree and the pair carries what they are.

The summary a pair stores is `RouteSummary`: metres per way class. That is deliberately
less than the route. A labelled set built this way is self-contained YAML - no fixtures, no
layer store, no router to replay - and it survives the OSM vintage underneath it changing,
which a stored route full of way ids would not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.core.routing.lts import lts_from_tags
from longrun.core.routing.tuning import PreferencePair, RouteSummary, WayClass

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from longrun.core.models.geometry import Segment

#: OSM `highway` values that `to_custom_model`'s path bonus matches on. Kept in step with
#: `road_class == PATH || road_class == FOOTWAY` there rather than with `lts.py`'s wider
#: `SEPARATED_HIGHWAYS`: this is the router's vocabulary, and fitting a parameter against
#: ways the router cannot recognise would produce a vector that does nothing.
PATH_HIGHWAYS = frozenset({"path", "footway"})

#: What the missing-sidewalk-on-collector multiplier matches: the two classes GraphHopper
#: calls SECONDARY and TERTIARY.
COLLECTOR_HIGHWAYS = frozenset({"secondary", "secondary_link", "tertiary", "tertiary_link"})

#: `surface` values the unpaved multiplier matches, as OSM spells them.
UNPAVED_SURFACES = frozenset(
    {"unpaved", "gravel", "dirt", "ground", "sand", "fine_gravel", "compacted", "earth", "grass"}
)


def way_class(tags: dict[str, Any] | None) -> WayClass:
    """The class a way falls into, from its OSM tags.

    A way with no tags is `WayClass()` - LTS unknown, nothing else true. That is scope 12's
    rule and it matters here more than usual: charging an unmatched way the LTS 2
    multiplier would fit a parameter against missing data and call the result a preference.
    """
    if not tags:
        return WayClass()
    highway = str(tags.get("highway") or "").lower()
    surface = str(tags.get("surface") or "").lower()
    lts = lts_from_tags(tags)
    return WayClass(
        lts=lts.level,
        unpaved=surface in UNPAVED_SURFACES,
        is_path=highway in PATH_HIGHWAYS,
        is_collector=highway in COLLECTOR_HIGHWAYS,
    )


def summarise(
    name: str,
    segments: Sequence[Segment],
    tags_by_way: dict[int, dict[str, Any]],
) -> RouteSummary:
    """A route as metres per way class, from segments the pipeline already matched.

    Built from segments rather than from the corridor's ways, because a corridor contains
    every way *near* the route and the fit is about the ways the route is *on*. Using the
    corridor would charge a vector for streets the runner never set foot on.
    """
    ways: list[tuple[WayClass, float]] = []
    for segment in segments:
        tags = None if segment.way_id is None else tags_by_way.get(int(segment.way_id))
        ways.append((way_class(tags), segment.length_m))
    return RouteSummary.from_ways(name, ways)


def pairs_from_edit(
    original: RouteSummary,
    edited: RouteSummary,
    *,
    weight: float = 1.0,
) -> list[PreferencePair]:
    """One pair: a runner took a route this project drew and changed it.

    The cheapest labelling interface this project will ever have, because it is a thing
    people already do - `longrun repair` exists to take an edited GPX. The edited route is
    preferred by construction: somebody spent effort making it, which is a stronger signal
    than any questionnaire, and is exactly the revealed preference scope 6.3 argues for
    when it refuses to ask questions up front.

    Returns an empty list when the two routes are the same shape. Nothing was rejected, so
    nothing was preferred, and a pair recording a choice nobody made would be noise the fit
    counts as evidence.
    """
    if original.metres_by_class == edited.metres_by_class:
        return []
    return [PreferencePair(a=original, b=edited, preferred="b", source="edit", weight=weight)]


def pairs_from_history(
    candidates: Sequence[tuple[RouteSummary, frozenset[int]]],
    accepted: frozenset[int],
    *,
    min_share: float = 0.25,
) -> list[PreferencePair]:
    """Pairs from the accepted-road set: routes a runner demonstrably runs (scope 6.2).

    Each candidate is a route summary and the way ids it uses. A route using more of the
    runner's accepted roads than its alternative is the one their own history says they
    run, and `accepted_ways` already produces that set from map-matched `osm_way_id`s -
    ADR 0001's retired-R4 decision, finally consumed by something.

    Weighted by the *difference* in accepted share, not by the share itself: two routes
    that both run entirely on familiar roads say nothing about the six parameters, and a
    pair that counted them equally with a clear-cut one would dilute the evidence.
    `min_share` is the floor below which a difference is not worth recording at all.
    """
    pairs: list[PreferencePair] = []
    for index, (summary_a, ways_a) in enumerate(candidates):
        for summary_b, ways_b in candidates[index + 1 :]:
            share_a = _accepted_share(ways_a, accepted)
            share_b = _accepted_share(ways_b, accepted)
            difference = abs(share_a - share_b)
            if difference < min_share:
                continue
            pairs.append(
                PreferencePair(
                    a=summary_a,
                    b=summary_b,
                    preferred="a" if share_a > share_b else "b",
                    source="history",
                    weight=difference,
                )
            )
    return pairs


def _accepted_share(ways: frozenset[int], accepted: frozenset[int]) -> float:
    """How much of a route's way set the runner has run at least twice.

    By way count rather than by metres, because the accepted set is a set of ids and
    nothing carries their lengths. Worth naming as an approximation rather than hiding:
    a route whose one long accepted way carries most of its distance scores the same as one
    with many short ones.
    """
    if not ways:
        return 0.0
    return len(ways & accepted) / len(ways)


__all__ = [
    "COLLECTOR_HIGHWAYS",
    "PATH_HIGHWAYS",
    "UNPAVED_SURFACES",
    "pairs_from_edit",
    "pairs_from_history",
    "summarise",
    "way_class",
]
