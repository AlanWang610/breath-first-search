"""Which jurisdictions a route crosses (scope 7.10, 13 step 4).

Two callers with two different queries. `regions/build.py` asks about a region polygon
against a live database; a scorer asks about a route against a frozen GeoPackage with no
database anywhere. What they share is the vocabulary in `core/models/jurisdiction.py` and
the two constructors here, which is what keeps them from becoming two implementations of
one idea.

**The route line, not the corridor rectangle.** `corridor_polygon` buffers the route's
*bounding box*, so a point-to-point route's corridor is a rectangle containing everything
between the endpoints - the Bay Area region polygon resolves to 155 places that way.
`lines_crossing` uses the real route LineString and is already on the `LayerStore`
protocol, implemented by both stores. It is what makes the phrase "jurisdictions crossed"
true rather than approximately true.

Parks are the exception and are asked with the corridor, because a park you run *beside*
still owns the path you are on and still posts the alert that closes it.

**Nothing here raises.** A fixture with no `boundaries` layer is the ordinary case for
every golden route committed before M4, and it produces a reason, not an exception. That
is scope 3.6 applied to the question of who owns the ground.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from longrun.core.models.jurisdiction import (
    Jurisdiction,
    is_unknown_agency,
    padus_id,
    tiger_id,
)

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route

#: TIGER levels, coarsest first. Order matters twice: `within` is built by walking it, and
#: a coarse jurisdiction has to be resolved before a finer one can be told it sits inside.
LEVELS: tuple[str, ...] = ("state", "county", "place")

#: How wide a corridor to ask the parks layer with. Wider than the scoring corridor on
#: purpose - a gate across the trail can sit a few hundred metres off the line.
PARK_BUFFER_M = 500.0


def from_tiger_row(level: str, geoid: str, name: str, statefp: str | None = None) -> Jurisdiction:
    """One TIGER boundary as a jurisdiction.

    `statefp` is a column on every row of `tiger.boundaries`, so a place's state is read
    rather than sliced off the front of its GEOID. The slice would be a silent wrong
    answer often enough to matter: county `29095` is a lexical prefix of place `2909512`.
    """
    within: list[str] = []
    if statefp and level != "state":
        within.append(tiger_id("state", statefp))
    return Jurisdiction(
        id=tiger_id(level, geoid),
        level=level,  # type: ignore[arg-type]
        name=name,
        within=tuple(within),
        source="tiger",
    )


def from_padus_row(
    agency: str | None,
    name: str,
    agency_type: str | None = None,
    statefp: str | None = None,
) -> Jurisdiction | None:
    """One PAD-US management area as a jurisdiction, or `None` if it names nobody.

    `None` rather than an "unknown" jurisdiction, because a park managed by
    nobody-in-particular is not a body an adapter can be registered against, and carrying
    it would put `padus:UNK` in every coverage manifest in the country.
    """
    if is_unknown_agency(agency):
        return None
    assert agency is not None  # narrowed by is_unknown_agency
    code = agency.strip().upper()
    within = (tiger_id("state", statefp),) if statefp else ()
    return Jurisdiction(
        id=padus_id(code, statefp),
        level="park",
        name=name or code,
        within=within,
        agency=code,
        agency_type=agency_type,
        source="padus",
    )


def _column(row: Any, name: str) -> str | None:
    """One field of a frame row, or `None` when the source never carried the column.

    `Series.get` returns `None` for a missing label rather than raising, which is the
    behaviour wanted here and the behaviour that caused a silent NULL load in M3 where it
    was not. A fixture frozen before a column existed - `synthetic-hazards/parks.geojson`
    has no `agency` at all - must read as unknown, not crash the scorer that opens it.
    """
    try:
        value = row.get(name)
    except (AttributeError, KeyError):  # pragma: no cover - defensive
        return None
    if value is None:
        return None
    # A null in a frame is NaN, not None, and `str(float("nan"))` is the *truthy* string
    # "nan" - so a missing GEOID would become a jurisdiction id of `tiger:county:nan` and
    # be reported as a jurisdiction crossed. Caught by the test for a row missing its geoid.
    if value != value:  # noqa: PLR0124 - the NaN test, and the only one that works here
        return None
    text = str(value).strip()
    return None if not text or text.lower() in {"nan", "none", "<na>"} else text


def jurisdictions_from_frames(
    boundaries: GeoDataFrame | None,
    parks: GeoDataFrame | None = None,
) -> list[Jurisdiction]:
    """Two frames in, jurisdiction records out. Pure, and the whole of the interesting part.

    Testable against hand-built rectangles with no store, no database and no fixture, which
    is what the two traps deserve: a park whose state cannot be resolved, and a place whose
    GEOID extends a county's.
    """
    found: dict[str, Jurisdiction] = {}

    if boundaries is not None and len(boundaries):
        for _, row in boundaries.iterrows():
            level = _column(row, "level")
            geoid = _column(row, "geoid")
            if level not in LEVELS or not geoid:
                continue
            name = _column(row, "name") or geoid
            record = from_tiger_row(level, geoid, name, _column(row, "statefp"))
            found.setdefault(record.id, record)

    # A park's state comes from the boundaries already resolved, so a park is qualified by
    # the state the *route* is in rather than by a spatial join nobody asked for. On a
    # state-line route there may be two, and a park matching neither stays unqualified and
    # says so.
    states = sorted({j.id.rsplit(":", 1)[-1] for j in found.values() if j.level == "state"})
    only_state = states[0] if len(states) == 1 else None

    if parks is not None and len(parks):
        for _, row in parks.iterrows():
            park = from_padus_row(
                _column(row, "agency"),
                _column(row, "name") or "",
                _column(row, "agency_type"),
                _column(row, "statefp") or only_state,
            )
            if park is not None:
                found.setdefault(park.id, park)

    return sorted(
        found.values(), key=lambda j: (LEVELS.index(j.level) if j.level in LEVELS else 9, j.id)
    )


@dataclass(frozen=True)
class JurisdictionScan:
    """Who owns the ground under a route, and which sources could say.

    `boundaries_checked` false is not "the route crossed no jurisdictions" - it is "nobody
    could be asked", and the two have to reach the plan sheet as different sentences.
    """

    jurisdictions: list[Jurisdiction] = field(default_factory=list)
    boundaries_checked: bool = False
    parks_checked: bool = False
    reasons: list[str] = field(default_factory=list)
    #: Park polygons met but not attributable to any agency - PAD-US rows whose `Mang_Name`
    #: is one of the "unknown" codes, or a fixture frozen before the column existed. Counted
    #: rather than dropped, because "no managed land here" and "managed land whose manager
    #: this data cannot name" are different sentences and only the first is an all-clear.
    unattributed_parks: int = 0

    @property
    def answered(self) -> bool:
        return self.boundaries_checked or self.parks_checked

    def of_level(self, level: str) -> list[Jurisdiction]:
        return [j for j in self.jurisdictions if j.level == level]


def route_jurisdictions(route: Route, ctx: ScorerContext) -> JurisdictionScan:
    """The store-backed wrapper. Never raises; a missing layer is a reason."""
    from longrun.core.geo.segments import corridor

    boundaries = None
    parks = None
    reasons: list[str] = []

    try:
        boundaries = ctx.layers.lines_crossing(route, "boundaries")
    except Exception as exc:  # noqa: BLE001 - an absent layer is an answer, not a failure
        reasons.append(f"no boundaries layer: {_describe(exc)}")

    try:
        parks = ctx.layers.polygons_intersecting(corridor(route, buffer_m=PARK_BUFFER_M), "parks")
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"no parks layer: {_describe(exc)}")

    found = jurisdictions_from_frames(boundaries, parks)
    named = sum(1 for j in found if j.source == "padus")
    return JurisdictionScan(
        jurisdictions=found,
        boundaries_checked=boundaries is not None,
        parks_checked=parks is not None,
        reasons=reasons,
        unattributed_parks=max(0, len(parks) - named) if parks is not None else 0,
    )


def _describe(exc: Exception) -> str:
    """A failure, named without leaking a path.

    `LayerNotFound` interpolates an absolute path, and a coverage reason carrying a Windows
    drive letter fails on CI - M2 shipped that bug into a golden expectation once already.
    """
    from longrun.core.data.file_store import LayerNotFound

    if isinstance(exc, LayerNotFound):
        return "not in this fixture"
    return type(exc).__name__


__all__ = [
    "LEVELS",
    "PARK_BUFFER_M",
    "JurisdictionScan",
    "from_padus_row",
    "from_tiger_row",
    "jurisdictions_from_frames",
    "route_jurisdictions",
]
