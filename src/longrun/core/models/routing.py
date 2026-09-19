"""How a plan asks the router for a line, and the turns it answers with (scope 6.4, 7.1, 7.2).

One place for the two things every routing call in a plan must carry, because until M8 they
were carried by one call and dropped by the rest. `cli/plan.py` built a costing model,
handed it to its own opening `route`, and the loop then called `alternatives` bare - so
`--avoid-high-stress` shaped the first line and nothing after it. `PlanRequest.avoid_polygons`
fared worse: declared in M1, plumbed end to end through `core/routing/`, and passed by
nobody.

`core/routing/base.py` states the consequence in its own protocol docstring: *"an
alternative drawn without the custom model the original was drawn with is not comparable to
it, and arbitrating between the two would be comparing two different questions."* Scope 8.4
then arbitrates between them, which is the bug wearing its consequences.

**Resolved once, and persisted.** The policy is built where a plan begins and stored on the
scratchpad, rather than recomputed per call. A pause survives the process (scope 4.2) and
`api/app.py` rebuilds a router on resume - a policy recomputed there could differ from the
one the first half of the plan used, and the two halves of a route would be costed
differently with nothing to show for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.routing.base import CostingModel


class RoutingPolicy(BaseModel):
    """The costing model and the areas every routing call in one plan must carry."""

    model_config = ConfigDict(frozen=True)

    #: The scope 7.1 custom model, as the router adapter's opaque object. `None` is the
    #: neutral model, which `to_custom_model` returns as an empty body - so `None` and "no
    #: terms" mean the same thing to the router and are deliberately not distinguished.
    custom_model: dict[str, Any] | None = None

    #: GeoJSON areas the route may not enter (scope 6.4). From `PlanRequest.avoid_polygons`
    #: and, once the geocoder is wired in, from `avoid_names`.
    avoid_polygons: list[dict[str, Any]] = Field(default_factory=list)

    #: What could not be resolved - an unreachable geocoder, a name with no match. Carried
    #: rather than raised: scope 3.6's rule is that an absent source is reported, and a plan
    #: that dropped an avoid the user asked for must say so rather than route through it
    #: silently.
    notes: list[str] = Field(default_factory=list)

    @property
    def areas(self) -> list[dict[str, Any]] | None:
        """The avoid list in the shape the router protocol takes: `None` when empty."""
        return list(self.avoid_polygons) or None

    def costing(self) -> CostingModel | None:
        """The custom model, typed as the router's opaque costing object."""
        return self.custom_model


NEUTRAL_POLICY = RoutingPolicy()

__all__ = ["NEUTRAL_POLICY", "RoutingPolicy"]


#: Why a plan holds no cue sheet. Four facts, four sentences, and never collapsed into "no
#: cues": a runner told "no turns" about a route with forty of them has been misinformed,
#: and each cause needs a different thing done about it.
NO_ROUTER = (
    "this route was supplied rather than drawn (repair mode), and turns are not synthesised "
    "from geometry - a bearing detector would invent street names and turn a measurement "
    "into a guess"
)
NOT_REQUESTED = "turn instructions were not requested; re-run with --cue-sheet"
NOT_RETURNED = "the router was asked for turn instructions and returned none"
REROUTED = (
    "the loop rerouted after the cues were drawn, so the turns on record describe a line "
    "this plan no longer holds"
)


class Cue(BaseModel):
    """One turn, located by distance along the route (scope 7.2)."""

    model_config = ConfigDict(frozen=True)

    #: The locator, and the only one.
    #:
    #: Never a point index. A router numbers its instructions against the points it sent,
    #: and `path_to_route` renormalizes and then densifies those - measured on a real
    #: GraphHopper answer, the final "Arrive at destination" indexes raw point 258 at
    #: 18,153.9 m and reads as 10,058.8 m if taken as a route index. 8.1 km early on an
    #: 18.2 km route, and a plausible number.
    cum_dist_m: float
    #: The engine's own manoeuvre code, kept beside the word so a consumer that knows the
    #: vocabulary is not forced through this module's English.
    sign: int | None = None
    #: `sign` as a word, or `f"sign {n}"` for one this build does not know. Never silently
    #: "straight" - an unrecognised turn is not a non-turn.
    manoeuvre: str = ""
    #: The engine's instruction text, verbatim. Not regenerated from `sign` and
    #: `street_name`: the router composes roundabout exits and ramp text this cannot.
    text: str = ""
    #: `None` is *unknown*, not *unnamed* - scope 12's rule. An unnamed way and a way whose
    #: name the extract does not carry are different facts.
    street_name: str | None = None
    lat: float = 0.0
    lon: float = 0.0
    #: Metres from this cue to the next, as the engine measured this instruction's span.
    distance_m: float = 0.0
    #: Scope 7.2's "ambiguity flags", and only what is measurable from the response alone.
    ambiguous: bool = False
    ambiguity: str | None = None


class CueSheet(BaseModel):
    """The turns, or which of four reasons there are none. Never two states at once.

    `checked=True` with an empty `cues` is a route that genuinely has no turns.
    `checked=False` is a cue sheet that was not produced, and `reason` says why. Collapsing
    those is exactly the failure `tools/runnability.py` refused to ship: a cue sheet with no
    turns in it, and a route with no turns, rendered identically.
    """

    cues: list[Cue] = Field(default_factory=list)
    checked: bool = False
    reason: str | None = None
    #: How far the line the cues were drawn on differs in length from the line the plan
    #: holds. Non-zero after map matching, which returns a different point list from the one
    #: the router drew. Reported rather than gated on: the honest thing is to say how far
    #: apart the two readings are, not to invent a threshold and fail above it.
    frame_shift_m: float = 0.0

    @property
    def turn_count(self) -> int:
        """Cues that are actually a turn - the arrival is not one."""
        return sum(1 for cue in self.cues if cue.manoeuvre != "arrive")

    @property
    def ambiguous(self) -> list[Cue]:
        return [cue for cue in self.cues if cue.ambiguous]
