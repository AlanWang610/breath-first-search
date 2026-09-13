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
    FEDERAL_AGENCY_CODES,
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


def _state_lookup(boundaries: Any) -> list[tuple[Any, str]]:
    """Boundary polygons paired with the state they belong to.

    A state row carries its own FIPS in `geoid`; a county or place carries it in `statefp`.
    Rows with neither, or with no geometry, cannot qualify anything and are left out.
    """
    if boundaries is None or not len(boundaries):
        return []
    out: list[tuple[Any, str]] = []
    for _, row in boundaries.iterrows():
        statefp = _column(row, "statefp")
        if statefp is None and _column(row, "level") == "state":
            statefp = _column(row, "geoid")
        geometry = getattr(row, "geometry", None)
        if statefp and geometry is not None and not geometry.is_empty:
            out.append((geometry, statefp))
    return out


def _state_of(geometry: Any, lookup: list[tuple[Any, str]]) -> str | None:
    """Which state a park sits in, by intersecting the boundaries already resolved.

    **This is a read, not a guess**, and the distinction is the whole point. Both frames are
    already in hand, so "which state is this park in" is a point-in-polygon test against
    polygons this plan has already fetched — not an inference from the route's states.

    Zero matches, or more than one, leaves the park unqualified. Zero is ordinary: `parks`
    comes from a corridor buffer and `boundaries` from the route line, so a park beside the
    route can sit in a county the route never enters. More than one is a park that genuinely
    straddles a state line, and naming either half would be wrong.
    """
    if geometry is None:
        return None
    try:
        states = {fips for shape, fips in lookup if shape.intersects(geometry)}
    except Exception:  # noqa: BLE001 - an invalid geometry is unqualified, never fatal
        return None
    return states.pop() if len(states) == 1 else None


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

    # A park's state is resolved against the boundary polygons already fetched, because
    # `padus.units` carries no `statefp` of its own and a non-federal code without a state
    # claims the whole country: `padus:CITY` would match a city-parks adapter in any of the
    # fifty. The route's own state is only the fallback, and only when there is exactly one
    # - which is precisely the case a state-line route does not have. Kansas City is what
    # found this: with Missouri and Kansas both in play the fallback yields nothing, and
    # four KC parks were reporting national scope.
    lookup = _state_lookup(boundaries)
    states = sorted({j.id.rsplit(":", 1)[-1] for j in found.values() if j.level == "state"})
    only_state = states[0] if len(states) == 1 else None

    if parks is not None and len(parks):
        for _, row in parks.iterrows():
            statefp = (
                _column(row, "statefp")
                or _state_of(getattr(row, "geometry", None), lookup)
                or only_state
            )
            park = from_padus_row(
                _column(row, "agency"),
                _column(row, "name") or "",
                _column(row, "agency_type"),
                statefp,
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
    #: Park agencies whose state could not be resolved, so their id claims national scope.
    #: `padus:CITY` matches a city-parks adapter in any of the fifty states, so this is a
    #: correctness hazard the moment such an adapter exists - reported, never inferred.
    unqualified_agencies: list[str] = field(default_factory=list)

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
    # A park agency that could not be tied to a state is reportable, not silent. Its id
    # claims national scope, so an adapter registered for one state's city parks would
    # match it - the coverage manifest has to say which agencies are in that state.
    unqualified = sorted(
        {
            j.agency or j.id
            for j in found
            if j.source == "padus" and not j.within and j.agency not in FEDERAL_AGENCY_CODES
        }
    )
    named = sum(1 for j in found if j.source == "padus")
    return JurisdictionScan(
        jurisdictions=found,
        boundaries_checked=boundaries is not None,
        parks_checked=parks is not None,
        reasons=reasons,
        unattributed_parks=max(0, len(parks) - named) if parks is not None else 0,
        unqualified_agencies=unqualified,
    )


def unqualified_reason(scan: JurisdictionScan) -> str | None:
    """The sentence a plan owes when a park agency could not be tied to a state.

    Written once here rather than three times in the scorers, because all three read the
    same jurisdiction set and the claim is about the set, not about closures or trails.
    """
    if not scan.unqualified_agencies:
        return None
    codes = scan.unqualified_agencies
    shown = ", ".join(codes[:4]) + ("..." if len(codes) > 4 else "")
    return (
        f"{len(codes)} park agency code{'' if len(codes) == 1 else 's'} could not be tied to "
        f"a state ({shown}); a non-federal PAD-US code without one names every such manager "
        f"in the country, so no adapter may be matched to it"
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
    "unqualified_reason",
    "route_jurisdictions",
]
