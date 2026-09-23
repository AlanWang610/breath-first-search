"""Which stretches of a route the tide governs (scope 7.6).

The prerequisite nobody had. Scope §7.6 asks `access_hours` for *"tide conflicts for beach
segments"*, and until this module nothing in the codebase could name a beach segment: there
is no `natural=beach` or `natural=coastline` handling in any scorer, and a tide client with
nothing to point it at is a client that runs on every route or on none.

**What the `ways` layer actually carries is the whole design constraint.**
`core/data/osm.py::WAY_KEYS` is `highway|railway|footway|cycleway`, and it is passed to
osmium's C++-side `KeyFilter`, so a `natural=coastline` or `natural=beach` way — the things
you would reach for first — is **never loaded at all**. Neither is the beach *area*: the
area pass filters on `AREA_KEYS`, which is the amenity vocabulary. What is loaded is every
way a runner could be on, with its full tag dict, which `PostGISLayerStore` expands into
flat columns. So the only coastal evidence available is a tag on a way the route lies on.

**Adding `natural` to `WAY_KEYS` was considered and rejected.** `KeyFilter` is a disjunction,
so one more key admits every `natural=water`, `natural=ridge`, `natural=tree_row` and
`natural=cliff` linestring in the extract into `osm.ways` — the table `way_matching`,
`segment_hostility`, `surface_profile` and `legality` all read as "ways a runner can be on".
That is a much larger change than a tide check, it costs a full region reload
(`core/data/osm.py` says so at `WAY_KEYS`), and it would be paid by four scorers to benefit
one. A `natural=coastline` *layer* of its own is the honest shape if this is ever wanted,
and §7.6's question — "is the ground under this segment covered at high water" — is a
question about the way, not about the distance to the nearest coastline.

**Two rules, and the third was rejected on evidence.**

*`tidal=yes` is a categorical statement by whoever mapped it* that the way is covered at
high water — a tidal causeway, a tidal ford, a strand crossing. ADR 0013's test for what a
scorer may flag without inventing a sign is exactly this: "the sign is already in the datum".

*`natural=beach` or `natural=shoal` co-tagged on a runnable way* is weaker. It says the
ground is beach; it does not say the tide takes it. So it is a separate class with its own
confidence and, in `access_hours`, its own tier.

*`surface=sand` alone was rejected as a third rule.* It is the obvious heuristic and it is
wrong in a way this repository can check: a desert track is `highway=track surface=sand`,
and `phoenix-heat` is a golden route. (Measured before writing this: no golden fixture
carries `sand`, `mud`, `shingle` or `shells` as a surface today, so the rule would have been
inert *and* would have stayed inert until the first Sonoran fixture that had one. A rule
whose false positive is invisible in the suite is worse than no rule.) A soft surface is
corroboration for a coastal tag, never evidence on its own, and there is no coastal tag on a
way without one of the two rules above.

**Absence is not zero, and here there are three answers.** `coastal_stretches` returns a
list, and an empty list means "the ways layer was read and no segment on this route is
tidal" — *measured as none*. A caller that could not read the layer at all must not call
this and report an empty list; it has to say "not checked", which is why this module raises
nothing and reads no store. The third answer, "a tidal stretch whose tide could not be
established", belongs to the tide client and not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.geometry import Segment

#: What a stretch is. `tidal` is a mapper's statement that the water covers it; `beach` is
#: a statement about the ground that leaves the water an inference.
CoastalKind = Literal["tidal", "beach"]

#: Spellings of `tidal=yes` seen in OSM. `1` and `true` are both in use; anything else -
#: including `tidal=no`, which is a positive statement that the way does *not* flood - is
#: not a tidal way.
TIDAL_VALUES: frozenset[str] = frozenset({"yes", "true", "1"})

#: `natural` values that put a runnable way on the beach. `shoal` is included because a
#: causeway across one is the same question; `sand` is not, because inland dune tracks
#: carry it and it is the desert false positive this module's docstring rejects.
BEACH_NATURAL: frozenset[str] = frozenset({"beach", "shoal"})

#: Confidence in each class, multiplied into flag severity the way `hazards` and `closures`
#: multiply adapter confidence (ADR 0013: confidence scales severity, never the tier).
#:
#: 0.9 rather than 1.0 for `tidal=yes` because the tag says *that* the way floods and never
#: *how far up the tide* it floods, so even a definitive tag leaves the threshold inferred.
TIDAL_CONFIDENCE = 0.9

#: 0.5 for a beach: the ground is beach and the water is an inference from it.
BEACH_CONFIDENCE = 0.5

_CONFIDENCE: dict[str, float] = {"tidal": TIDAL_CONFIDENCE, "beach": BEACH_CONFIDENCE}


def coastal_kind(tags: dict[str, Any] | None) -> CoastalKind | None:
    """Whether the tide governs this way, and on what evidence.

    `None` for the overwhelmingly common case and for a segment whose way is not in the
    layer — both of which mean "nothing here says the tide matters", which is not the same
    as "the tide does not matter" and is why `access_hours` keeps the two apart by only
    ever asking about ways it has.
    """
    if not tags:
        return None
    if str(tags.get("tidal", "")).strip().lower() in TIDAL_VALUES:
        return "tidal"
    if str(tags.get("natural", "")).strip().lower() in BEACH_NATURAL:
        return "beach"
    return None


@dataclass(frozen=True)
class CoastalStretch:
    """A contiguous run of segments the tide governs, with where it starts and ends.

    A *stretch* rather than a point, and that is the whole reason this type exists. A gate
    is a point a runner arrives at; a tidal causeway is 800 m of ground that is passable or
    is not, and a tide station is tens of kilometres away governing all of it. Nothing about
    that fits `access_hours._place`, which drops a feature past `GATE_REACH_M = 200 m`.
    """

    kind: CoastalKind
    segment_ids: tuple[str, ...]
    cum_start_m: float
    cum_end_m: float
    #: Route-point index nearest the middle of the stretch, for placing the station query.
    #: An index rather than a `LatLon` so this module stays free of geometry and of the
    #: rounding `core.data.cache.COORD_PRECISION` applies at the cache key.
    mid_route_index: int

    @property
    def length_m(self) -> float:
        return self.cum_end_m - self.cum_start_m

    @property
    def mid_cum_m(self) -> float:
        return (self.cum_start_m + self.cum_end_m) / 2.0

    @property
    def confidence(self) -> float:
        return _CONFIDENCE[self.kind]


def coastal_stretches(
    segments: list[Segment], by_way: dict[int, dict[str, Any]]
) -> list[CoastalStretch]:
    """Contiguous runs of tide-governed segments, in route order.

    **A run breaks on any segment that is not the same kind**, including one whose way is
    simply missing from the layer. That is deliberate and it under-claims: a causeway split
    by one unmatched segment becomes two stretches and asks the same station twice. The
    alternative — bridging a gap of unknown ground — would extend a tidal claim across
    ground nothing said was tidal, which is the failure scope §12 names.

    **No minimum length.** `surface_profile` requires a sustained run because forty metres
    of gravel is not a surface problem; forty metres of tidal causeway is exactly as
    impassable at high water as four hundred, so there is nothing for a threshold to buy.
    """
    from longrun.core.scorers._common import segment_tags

    stretches: list[CoastalStretch] = []
    run: list[Segment] = []
    run_kind: CoastalKind | None = None

    def flush() -> None:
        if run_kind is None or not run:
            return
        middle = run[len(run) // 2]
        stretches.append(
            CoastalStretch(
                kind=run_kind,
                segment_ids=tuple(s.id for s in run),
                cum_start_m=run[0].cum_start_m,
                cum_end_m=run[-1].cum_end_m,
                mid_route_index=middle.start_idx,
            )
        )

    for segment in segments:
        kind = coastal_kind(segment_tags(segment, by_way))
        if kind != run_kind:
            flush()
            run, run_kind = [], kind
        if kind is not None:
            run.append(segment)
    flush()

    return stretches


__all__ = [
    "BEACH_CONFIDENCE",
    "BEACH_NATURAL",
    "TIDAL_CONFIDENCE",
    "TIDAL_VALUES",
    "CoastalKind",
    "CoastalStretch",
    "coastal_kind",
    "coastal_stretches",
]
