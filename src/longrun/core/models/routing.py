"""How a plan asks the router for a line (scope 6.4, 7.1).

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
