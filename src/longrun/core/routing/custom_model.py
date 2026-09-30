"""The query-time costing model: six fitted parameters and one profile term.

Scope 7.1 states the shape:

> `priority` is a function of ~6 parameters: LTS 2/3/4 multipliers, unpaved multiplier,
> path/footway bonus, missing-sidewalk-on-collector multiplier. Surface and hills terms
> are set from the preference profile at query time.
> Hard exclude `access=private`, `foot=no`, `highway=motorway`, railway ROW.

Until this module existed all of that was one dict literal in `cli/plan.py` carrying a
single term (`lts >= 3` times 0.2), so five of the six parameters had never been written
down anywhere a fitter could reach them. The point of naming them is not tidiness: **a
parameter that does not reach the router is a knob attached to nothing**, and an optimiser
handed one will assign it a value and report an improvement.
`test_every_parameter_reaches_the_router` reads the emitted body.

Two of the scope's clauses come out differently once the deployed graph is read rather
than assumed, and both are recorded here because they are the kind of thing that otherwise
becomes a silent routing failure:

**The hard excludes are already enforced, in the server profile, and are not repeated
here.** `deploy/graphhopper/config-*-lts.yml`'s `foot` profile blocks `!foot_access` (which
is what `foot=no` becomes), `road_access == PRIVATE`, `road_class == MOTORWAY` and
`hike_rating >= 2`. A query-time custom model is *merged* with the profile's, so those
terms apply to every request whether or not this module mentions them. Emitting them again
would change the request body — and `CachedRouter` keys on the body, so a duplicate would
miss every route this project has recorded, for no change in where the route goes.
`test_the_server_profile_still_carries_the_hard_excludes` reads the config file, so the two
cannot drift apart quietly.

**There is no hills term, because the graph has no elevation.**
`graph.elevation.provider` is commented out in every deployed config, so `average_slope` is
not among `graph.encoded_values` and a model naming it is rejected — which surfaces as a
routing error, i.e. as "no route exists", which is the wrong answer to "wrong graph".
Elevation reaches this project through `core/geo/dem.py` and the grade scorer, not through
the router. So the hills preference is scored, never routed, and that is the same split ADR
0001 drew between LTS scoring and LTS routing, for the same reason.

The `lts` encoded value is ADR 0001's, present only on a graph built by the LTS importer.
`to_custom_model` takes `has_lts` and drops those three terms when it is false, for the
same reason the slope term is absent entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.profile import PreferenceProfile
    from longrun.core.routing.base import CostingModel

#: Multipliers below this are an exclusion rather than a preference. A fitter that drove a
#: term to 0.001 would be blocking a road class while reporting a taste, and the difference
#: matters to anybody reading why a route went the way it did.
MIN_MULTIPLIER = 0.01

#: Above this a single term swamps every other in the router's search and the fit stops
#: being about trade-offs at all.
MAX_MULTIPLIER = 5.0

#: The six fields, in the order `as_vector` and the grid use. Named once so a fitter, the
#: sheet and the round-trip test cannot disagree about what "the six parameters" are.
PARAMETER_NAMES: tuple[str, ...] = (
    "lts2",
    "lts3",
    "lts4",
    "unpaved",
    "path_bonus",
    "missing_sidewalk",
)

#: GraphHopper `surface` values that mean "not a made surface". Wider than the three the
#: first draft of this module used: `UNPAVED` and `GROUND` are both common in OSM and
#: omitting them would have left the unpaved multiplier silently not applying to most of
#: the ways it is about.
#:
#: M23 added the last three, and the reason is the same failure one layer down: the fitter
#: (`preferences.UNPAVED_SURFACES`) counted `compacted`, `fine_gravel` and `grass` as
#: unpaved and this list did not, so a vector fitted to prefer Marin's compacted fire roads
#: would have been costed on ways its own `unpaved` term never touched. All three are
#: `Surface` enum names in GraphHopper 11 (read from the jar, not assumed), and the order
#: appends rather than sorts so the emitted condition changes only by what was added.
UNPAVED_SURFACES: tuple[str, ...] = (
    "UNPAVED",
    "GRAVEL",
    "DIRT",
    "GROUND",
    "SAND",
    "COMPACTED",
    "FINE_GRAVEL",
    "GRASS",
)

#: OSM spellings GraphHopper folds into one of its own `Surface` values on import -
#: `Surface.SURFACE_MAP` in the 11.0 jar, disassembled rather than remembered. A way tagged
#: `surface=earth` is `DIRT` to the router, so it has to be unpaved to the fitter too.
SURFACE_ALIASES: dict[str, str] = {
    "metal": "PAVED",
    "sett": "COBBLESTONE",
    "unhewn_cobblestone": "COBBLESTONE",
    "earth": "DIRT",
    "pebblestone": "GRAVEL",
    "grass_paver": "GRASS",
}

#: The `road_class` values the path bonus matches, and the ones the missing-sidewalk
#: multiplier does. Named so the emitted conditions and anything that *counts* ways by
#: class - a fitter, a route summary - read one list rather than two that agree today.
PATH_ROAD_CLASSES: tuple[str, ...] = ("PATH", "FOOTWAY")
COLLECTOR_ROAD_CLASSES: tuple[str, ...] = ("SECONDARY", "TERTIARY")


def osm_unpaved_surfaces() -> frozenset[str]:
    """`UNPAVED_SURFACES` as OSM spells them, aliases included."""
    return frozenset(value.lower() for value in UNPAVED_SURFACES) | frozenset(
        alias for alias, target in SURFACE_ALIASES.items() if target in UNPAVED_SURFACES
    )


def _any_of(field: str, values: tuple[str, ...]) -> str:
    return " || ".join(f"{field} == {value}" for value in values)


class CustomModelParams(BaseModel):
    """The six parameters scope 7.1 fits, and nothing else.

    Every field is a `priority` multiplier: 1.0 is indifference, below 1.0 avoids, above
    1.0 seeks. Defaults are all 1.0 - see `NEUTRAL`.
    """

    model_config = ConfigDict(frozen=True)

    lts2: float = Field(default=1.0, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    lts3: float = Field(default=1.0, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    lts4: float = Field(default=1.0, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    unpaved: float = Field(default=1.0, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    path_bonus: float = Field(default=1.0, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    missing_sidewalk: float = Field(default=1.0, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)

    @property
    def is_neutral(self) -> bool:
        """Whether this expresses no preference at all.

        Read by the plan sheet: a route drawn with a neutral model was drawn by stock
        `foot_priority`, and saying so is the difference between "we tuned this" and "we
        did not", which risk R1 makes a live question.
        """
        return all(getattr(self, name) == 1.0 for name in PARAMETER_NAMES)

    def as_vector(self) -> tuple[float, ...]:
        """The six values in `PARAMETER_NAMES` order, for a fitter with no field names."""
        return tuple(float(getattr(self, name)) for name in PARAMETER_NAMES)

    @classmethod
    def from_vector(cls, values: tuple[float, ...] | list[float]) -> CustomModelParams:
        if len(values) != len(PARAMETER_NAMES):
            raise ValueError(f"expected {len(PARAMETER_NAMES)} parameters, got {len(values)}")
        return cls(**dict(zip(PARAMETER_NAMES, (float(value) for value in values), strict=True)))


#: What ships. Every multiplier 1.0, so the router's own `foot_priority` decides.
#:
#: ADR 0021, and risk R1 is the measurement behind it: M0 compared `avoid` against
#: `neutral` on four route pairs and found a detour of at most 0.8%, because stock
#: `foot_priority` already keeps pedestrians off arterials. Shipping a non-neutral default
#: would bake in an assumption nobody has measured, which is what R1 warns against.
NEUTRAL = CustomModelParams()

#: The one non-neutral vector this project has used, preserved because
#: `--avoid-high-stress` is a documented flag and M5.4 measured what it does. Opt-in.
AVOID_HIGH_STRESS = CustomModelParams(lts3=0.2, lts4=0.2)


def _lts_terms(params: CustomModelParams) -> list[dict[str, str]]:
    """The three LTS multipliers, emitted only when they say something.

    A term of 1.0 is omitted rather than written as `multiply_by: "1"`. Two reasons, and
    the second is the one that bites: a shorter body is easier to read in a cassette, and
    `CachedRouter` keys on the body, so emitting no-op terms would make a neutral request
    hash differently from a request with no model and miss its own recording.
    """
    terms: list[dict[str, str]] = []
    for level, value in ((2, params.lts2), (3, params.lts3), (4, params.lts4)):
        terms.extend(_term(f"lts == {level}", value))
    return terms


def _term(condition: str, value: float) -> list[dict[str, str]]:
    """One rule saying "ways matching `condition` are worth `value` times the rest".

    **GraphHopper refuses a priority above 1** - "maximum of value '4' cannot be larger than
    1" - and it refuses the whole request, which reaches a caller as "no pedestrian route".
    Until M23 a seek (`value > 1`) was emitted as `multiply_by: "4"`, so every vector that
    sought anything, and every profile saying `surface: dirt`, was a request the router
    turned down; found the first time one was sent to a live graph. It had never been sent
    because nothing but the fitter's grid produces one and nothing consumes a fitted vector.

    So a seek is written as an avoid on the complement: "paths are worth 4x the rest" is
    `!(paths) * 0.25`. Every ratio between two ways is the same, which is all a priority
    means and all `tuning.cost` reasons about, and no way's priority exceeds the server's
    own. What is not identical is the ratio to `distance_influence`, which scales the other
    way - the cost of a route is a priority-weighted time plus a distance term, and making
    the rest dearer is not the same as making paths cheaper. That is the only form the
    server accepts, and the direction it errs in is towards the runner's preference.
    """
    if value == 1.0:
        return []
    if value < 1.0:
        return [{"if": condition, "multiply_by": _number(value)}]
    return [{"if": f"!({condition})", "multiply_by": _number(1.0 / value)}]


def _number(value: float) -> str:
    """A multiplier as GraphHopper wants it: a string, and never in exponent form.

    `str(1e-05)` is `"1e-05"`, which the expression parser rejects. `MIN_MULTIPLIER` keeps
    values above that today, but the formatting is pinned here rather than relying on the
    bound to stay where it is.
    """
    return f"{round(value, 4):g}"


def effective_params(
    params: CustomModelParams, profile: PreferenceProfile | None
) -> CustomModelParams:
    """The multipliers the router actually applies: the fitted ones, with the profile's
    stated surface term multiplied on (scope 7.1 splits them; see `_surface_terms`).

    One function because two readers need the same answer. `_surface_terms` emits these,
    and `tuning.fit` has to cost pairs with them too: a runner who stated "dirt" already has
    unpaved ways rewarded at query time, and a fit that ignored that would learn the same
    preference again and have it applied twice. Clamped to the multiplier bounds, which the
    emitted terms were not below `MIN_MULTIPLIER` until M23 - a stated "paved" on a fitted
    0.01 wrote 0.0001, an exclusion wearing a preference's clothes.
    """
    unpaved, path_bonus = params.unpaved, params.path_bonus
    if profile is not None:
        surface, weight = profile.surface.value, profile.surface.weight
        if surface == "dirt":
            unpaved *= 1.0 + weight
            path_bonus *= 1.0 + weight
        elif surface == "paved":
            unpaved *= max(MIN_MULTIPLIER, 1.0 - weight)

    def bounded(value: float) -> float:
        return min(max(value, MIN_MULTIPLIER), MAX_MULTIPLIER)

    return params.model_copy(
        update={"unpaved": bounded(unpaved), "path_bonus": bounded(path_bonus)}
    )


def _surface_terms(
    params: CustomModelParams, profile: PreferenceProfile | None
) -> list[dict[str, str]]:
    """The unpaved multiplier and the path bonus, with the profile's surface term on top.

    Scope 7.1 splits these deliberately: `unpaved` and `path_bonus` are *fitted*, and the
    surface preference is *stated*. A runner who says "dirt" is not making a claim the
    fitter should overrule, so the profile term multiplies the fitted one rather than
    replacing it - and a profile that says nothing leaves the fitted value exactly as it
    was, which is what keeps a neutral vector emitting no term at all.
    """
    applied = effective_params(params, profile)
    unpaved, path_bonus = applied.unpaved, applied.path_bonus

    return [
        *_term(_any_of("surface", UNPAVED_SURFACES), unpaved),
        *_term(_any_of("road_class", PATH_ROAD_CLASSES), path_bonus),
    ]


def _sidewalk_terms(params: CustomModelParams) -> list[dict[str, str]]:
    """Scope 7.1's sixth parameter: the missing-sidewalk-on-collector multiplier.

    GraphHopper has no sidewalk encoded value, so the condition names the class this is
    about: a collector with no separate footway is what puts a runner on the carriageway.
    `lts.py` reads `sidewalk=*` off the OSM tags directly and reaches a better answer; the
    router gets the class-level approximation and the plan sheet gets the real one. That is
    the LTS-scoring / LTS-routing split ADR 0001 drew, for the same reason.
    """
    return _term(_any_of("road_class", COLLECTOR_ROAD_CLASSES), params.missing_sidewalk)


def to_custom_model(
    params: CustomModelParams = NEUTRAL,
    profile: PreferenceProfile | None = None,
    *,
    has_lts: bool = True,
) -> CostingModel:
    """Build the GraphHopper custom model this parameter vector and profile imply.

    Returns `{}` when there is nothing to say, because an empty model and an absent one
    must reach the router as the same request: `route_body` omits `custom_model` for a
    falsy model, and `CachedRouter` keys on the body. That is what keeps a neutral plan
    replaying against a cassette recorded before this module existed.

    The hard excludes are not here. They are in the server profile, which a query-time
    model is merged with rather than replacing - see this module's docstring.
    """
    priority: list[dict[str, str]] = []
    if has_lts:
        priority.extend(_lts_terms(params))
    priority.extend(_surface_terms(params, profile))
    priority.extend(_sidewalk_terms(params))
    return {"priority": priority} if priority else {}


def describe(params: CustomModelParams) -> str:
    """One line for the plan sheet, naming the vector the route was drawn with.

    Risk R1 is why this is rendered rather than stored silently: whether these parameters
    help is an open question, so a plan says which of them were applied to it.
    """
    if params.is_neutral:
        return "neutral (stock foot_priority; the six scope 7.1 parameters are unfitted)"
    return ", ".join(
        f"{name}={value:g}"
        for name, value in zip(PARAMETER_NAMES, params.as_vector(), strict=True)
        if value != 1.0
    )


__all__: list[str] = [
    "AVOID_HIGH_STRESS",
    "MAX_MULTIPLIER",
    "MIN_MULTIPLIER",
    "NEUTRAL",
    "PARAMETER_NAMES",
    "UNPAVED_SURFACES",
    "SURFACE_ALIASES",
    "PATH_ROAD_CLASSES",
    "COLLECTOR_ROAD_CLASSES",
    "osm_unpaved_surfaces",
    "effective_params",
    "CustomModelParams",
    "describe",
    "to_custom_model",
]
