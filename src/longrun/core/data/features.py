"""Adapter features read on the route's clock (scope 7.6, 7.10; ADR 0045, ADR 0046).

A feature's window is an **instant**, and it is stored as naive UTC like every other
instant this project keeps (ADR 0045's first rule). An ETA is a naive **local** wall clock.
The two are not comparable without the route's offset, and the question ADR 0045 answered
for the forecast - *who* makes the conversion - has the same answer here: the data layer,
once, and not each scorer.

That is not a hypothetical. Until M17 `wzdx.feed.parse_timestamp` dropped the offset rather
than converting it, so a Missouri work zone ending `14:00Z` was read as ending at 14:00 in
Kansas City - five hours late - by all three scorers that read features, and none of them
could have noticed, because each compared the numbers it was given. Put behind
`RouteFeatures.active_at`, no scorer does the arithmetic and a fourth cannot get it wrong.

**`None` means the clock could not be read, never "open".** A route with no points has no
coordinate to resolve an offset against, and `active_at` then answers `None` for any record
with a bounded window. Each scorer already has an "arrival time unknown" branch, and that is
the branch this reaches - absence is not zero (scope 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.features import Feature, FeatureKind, FeatureSet
    from longrun.core.models.geometry import Route
    from longrun.core.models.jurisdiction import Jurisdiction


@dataclass(frozen=True)
class RouteFeatures:
    """What the registry answered, and the clock to read its windows on."""

    found: FeatureSet
    #: Hours from UTC for this route's ETAs, as `route_offset` resolved it; `None` when the
    #: route has no coordinate to resolve against.
    utc_offset_hours: float | None
    #: How the offset was found - stated, a zone, or longitude - per ADR 0008.
    offset_source: str | None = None

    def to_utc(self, local: datetime) -> datetime | None:
        """A naive local ETA as the naive UTC instant it names, or `None` with no offset."""
        if self.utc_offset_hours is None:
            return None
        return local - timedelta(hours=self.utc_offset_hours)

    def to_local(self, utc: datetime) -> datetime | None:
        """A naive UTC instant as a naive local wall clock, or `None` with no offset."""
        if self.utc_offset_hours is None:
            return None
        return utc + timedelta(hours=self.utc_offset_hours)

    def active_at(self, feature: Feature, local_eta: datetime) -> bool | None:
        """Whether a record applies when the runner arrives, or `None` if nobody can say.

        A record with no bounds at all applies at every instant, so it needs no clock and
        is answered even when the offset is unknown.
        """
        if feature.start is None and feature.end is None:
            return True
        instant = self.to_utc(local_eta)
        if instant is None:
            return None
        return feature.active_at(instant)


def route_features(
    kind: FeatureKind,
    jurisdictions: Sequence[Jurisdiction],
    route: Route,
    ctx: ScorerContext,
) -> RouteFeatures:
    """Ask the registry about this route, and resolve the clock its answer is read on.

    The corridor polygon and the plan's local date are computed here rather than by each
    caller, because all three callers computed them identically and the offset belongs
    beside them. `ctx.features` must not be `None`; the scorers report that case themselves
    because it is a different sentence ("no adapter registry configured").
    """
    from longrun.core.data.forecast import route_offset
    from longrun.core.geo.segments import corridor, corridor_polygon

    assert ctx.features is not None, "route_features needs a registry; callers check first"
    polygon: Any = corridor_polygon(corridor(route))
    found = ctx.features.fetch(kind, jurisdictions, polygon, ctx.clock.now().date())
    hours, how = route_offset(route, ctx)
    return RouteFeatures(found=found, utc_offset_hours=hours, offset_source=how)


__all__ = ["RouteFeatures", "route_features"]
