"""The scorer contract (scope 3.2, 3.4, 3.6).

Every scorer is a pure function `(route, segments, ctx, etas) -> ScorerResult`. It
measures and it flags threshold crossings; it never decides whether a measurement is good
or bad. The sign lives in the preference profile, and only safety floors are
preference-independent.

Two helpers here carry most of scope 3.6. `unavailable()` is how a scorer says "this
source could not be consulted" — a real answer, recorded in the coverage manifest, and
categorically different from "I checked and there was nothing". `record_coverage()` is how
a scorer that *did* run says so. A scorer that returns neither has told the plan sheet
nothing, and that is the failure mode the coverage tests exist to catch.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.coverage import Tier as AdapterTier
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import ScorerResult

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext


class Scorer(Protocol):
    """What every scorer looks like from the outside."""

    name: str

    def __call__(
        self,
        route: Route,
        segments: list[Segment],
        ctx: ScorerContext,
        etas: list[datetime] | None = None,
    ) -> ScorerResult: ...


def unavailable(
    name: str,
    reason: str,
    kind: str | None = None,
    jurisdiction: str | None = None,
    tier: AdapterTier | None = None,
) -> ScorerResult:
    """A scorer that could not run, reported honestly rather than as a clean pass.

    This is not a stub in the pejorative sense. Scope 3.6 makes "report coverage
    honestly" a design principle, and a plan whose closure scorer silently returned no
    flags because no adapter exists is a plan that lies. The empty result plus a
    `checked=False` entry is the truthful form of that answer.
    """
    return ScorerResult(
        name=name,
        coverage=[
            CoverageEntry(
                source=name,
                kind=kind or name,
                checked=False,
                jurisdiction=jurisdiction,
                tier=tier,
                reason=reason,
            )
        ],
    )


def record_coverage(
    result: ScorerResult,
    source: str,
    kind: str,
    vintage: str | None = None,
    confidence: float | None = None,
    *,
    jurisdiction: str | None = None,
    tier: AdapterTier | None = None,
) -> ScorerResult:
    """Attach a `checked=True` entry to a scorer that did consult a source.

    `jurisdiction` and `tier` are keyword-only and arrived with M4: `unavailable()` has
    accepted both since M1, so a scorer could say which jurisdiction it *failed* to check
    and had no way to say which one it succeeded at. Keyword-only so that none of the
    existing positional call sites move.
    """
    result.coverage.append(
        CoverageEntry(
            source=source,
            kind=kind,
            checked=True,
            jurisdiction=jurisdiction,
            tier=tier,
            vintage=vintage,
            confidence=confidence,
        )
    )
    return result


def publish(ctx: ScorerContext, result: ScorerResult) -> ScorerResult:
    """Copy a scorer's coverage entries into the plan-wide manifest.

    Coverage is a sink (scope 3.6): the manifest on the context is the single place the
    plan sheet reads from, so a result's entries have to reach it. Scorers return their
    own entries too, which keeps them independently testable without a context.
    """
    for entry in result.coverage:
        ctx.coverage.record(entry)
    return result
