"""ADR 0020's deferred pass: each judged pair re-drawn once with the fitted vector (ADR 0051).

A fit scores a vector by costing lines that already exist (ADR 0020), which is fast and
which cannot see two things the router does:

* **A vector the router refuses.** Every vector that sought anything was a request
  GraphHopper turned down until M23's correction, and costing never noticed, because the
  cost of a line is arithmetic and the arithmetic was fine.
* **What the router actually draws.** A seek is sent as an avoid on the complement, and the
  router's cost adds a distance term that a priority does not scale - so the line a vector
  draws is not certain to be the one its costing prefers.

So before a vector is saved, each pair's ends are routed once with it - one call per pair,
which ADR 0020 deferred rather than forbade - and the line that comes back is compared with
the two the runner was shown. It lands on the one they chose, on the one they turned down,
or on neither (a third line nobody judged). The router is injected, as in `seeding`, so
nothing here opens a socket.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from longrun.core.labels.pairs import MAX_SHARED_FRACTION, PairTask
from longrun.core.labels.review import route_of
from longrun.core.labels.seeding import shared_fraction
from longrun.core.models.geometry import LatLon
from longrun.core.routing.base import RouterUnavailable, RoutingError

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.routing.base import CostingModel, Router
    from longrun.core.routing.tuning import PreferencePair

Outcome = Literal["chosen", "rejected", "neither", "refused"]


@dataclass(frozen=True)
class Redrawn:
    """One pair's ends, routed with the vector being checked."""

    task_id: str
    outcome: Outcome
    #: The re-drawn line's length over the shorter of the two the runner was shown.
    ratio: float | None = None
    #: Why the router refused, when it did.
    reason: str | None = None


@dataclass
class RerouteReport:
    """What the router drew with a vector, against what the runner chose."""

    rows: list[Redrawn] = field(default_factory=list)
    #: Pairs whose task is not in this ledger - an edit pair, or answers from another
    #: machine. Their ends are not here, so they cannot be routed.
    unchecked: int = 0
    tolerance_pct: float = 10.0

    def count(self, outcome: Outcome) -> int:
        return sum(1 for row in self.rows if row.outcome == outcome)

    @property
    def over_tolerance(self) -> list[Redrawn]:
        """Re-drawn lines longer than the runner's detour tolerance allows.

        The re-route's half of the detour penalty a saved fit must have at zero: a vector
        whose costed choices stay within tolerance and whose drawn lines do not is buying
        agreement with detours in the router rather than in the fit.
        """
        allowed = 1.0 + self.tolerance_pct / 100.0
        return [row for row in self.rows if row.ratio is not None and row.ratio > allowed]

    def problems(self) -> list[str]:
        """Why this vector may not be saved; empty when it may."""
        out = []
        if not self.rows:
            out.append("no pair has its ends in this ledger, so none could be re-drawn")
        refused = [row for row in self.rows if row.outcome == "refused"]
        if refused:
            out.append(
                f"the router refused {len(refused)} re-drawn pair(s) "
                f"(first: {refused[0].task_id}: {refused[0].reason})"
            )
        chosen, rejected = self.count("chosen"), self.count("rejected")
        if self.rows and chosen <= rejected:
            out.append(
                f"re-drawn, the vector lands on the line the runner chose {chosen} time(s) "
                f"and on the one they turned down {rejected}: not more often"
            )
        over = self.over_tolerance
        if over:
            worst = max(over, key=lambda row: row.ratio or 0.0)
            out.append(
                f"{len(over)} re-drawn line(s) beyond the {self.tolerance_pct:g}% detour "
                f"tolerance (worst {worst.task_id}: {((worst.ratio or 1.0) - 1) * 100:.0f}% "
                "over the shorter line on offer)"
            )
        return out

    @property
    def passed(self) -> bool:
        return not self.problems()

    def __str__(self) -> str:
        skipped = f"; {self.unchecked} pair(s) not in this ledger" if self.unchecked else ""
        verdict = "passes" if self.passed else "does not pass"
        return (
            f"re-drawn {len(self.rows)} pair(s) with the fitted vector: "
            f"{self.count('chosen')} on the chosen line, {self.count('rejected')} on the "
            f"rejected one, {self.count('neither')} on neither, {self.count('refused')} "
            f"refused{skipped} - {verdict}"
        )


def reroute(
    pairs: Sequence[PreferencePair],
    tasks: Sequence[PairTask],
    router_for: Callable[[PairTask], Router],
    custom_model: CostingModel | None,
    *,
    tolerance_pct: float = 10.0,
) -> RerouteReport:
    """Route each pair's ends once with `custom_model` and say where the line landed.

    A pair finds its task by its two candidate ids, which is all a published pair carries
    (ADR 0049). `RouterUnavailable` is raised rather than counted: a router that is not
    running says nothing about the vector, and a check that counted it as a refusal would
    blame the fit for a stopped container.
    """
    by_ids = {frozenset((task.a.id, task.b.id)): task for task in tasks}
    report = RerouteReport(tolerance_pct=tolerance_pct)
    for pair in pairs:
        task = by_ids.get(frozenset((pair.a.name, pair.b.name)))
        if task is None:
            report.unchecked += 1
            continue
        ends = [LatLon(lat=lat, lon=lon) for lat, lon in task.ends]
        try:
            line = router_for(task).route(ends, custom_model=custom_model)
        except RouterUnavailable:
            raise
        except RoutingError as exc:
            report.rows.append(Redrawn(task_id=task.id, outcome="refused", reason=str(exc)))
            continue
        chosen, rejected = (task.a, task.b) if task.a.id == pair.chosen.name else (task.b, task.a)
        # The pairs' own rule for "the same route" (`pairs.distinct`): more than 85% of the
        # re-drawn line along a candidate is that candidate with a wiggle. Below it on both,
        # the router drew a third line, which is neither agreement nor disagreement.
        on_chosen = shared_fraction(line, route_of(chosen))
        on_rejected = shared_fraction(line, route_of(rejected))
        outcome: Outcome = "neither"
        if max(on_chosen, on_rejected) > MAX_SHARED_FRACTION:
            outcome = "chosen" if on_chosen >= on_rejected else "rejected"
        shorter = min(task.a.length_m, task.b.length_m)
        report.rows.append(
            Redrawn(
                task_id=task.id,
                outcome=outcome,
                ratio=round(line.length_m / shorter, 4) if shorter > 0 else None,
            )
        )
    return report


__all__ = ["Outcome", "Redrawn", "RerouteReport", "reroute"]
