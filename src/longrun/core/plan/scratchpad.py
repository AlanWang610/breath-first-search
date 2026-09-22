"""The scratchpad: the loop's working state, persisted (scope 4.4, 8.1).

Scope 4.2 rejects a graph-workflow framework on the grounds that the control flow is
known in advance and human-in-the-loop pauses can be handled by persisting state and
returning `needs_input`. This module is the whole of that mechanism: the loop writes here
between tool calls, and a job that stops to ask a question resumes by reloading it.

The scratchpad plus the manifest is the stored plan, which is what `refresh_plan` and
`route_diff` operate on later.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import ScorerResult
from longrun.core.models.plan import Manifest, PendingQuestion, TradeOff
from longrun.core.models.profile import PreferenceProfile
from longrun.core.models.request import LockedRange, LockSource, PlanRequest
from longrun.core.models.routing import RoutingPolicy

JobStatus = Literal["running", "needs_input", "complete", "failed"]


class Scratchpad(BaseModel):
    """Everything the loop needs to pick up where it left off."""

    plan_id: str
    request: PlanRequest
    #: Optional, because a job can stop before it has a route. Scope 6.3's two first-use
    #: questions and any intent clarification come *before* scope 8.1 step 3, and a
    #: scratchpad that could not hold them would have nothing to persist at the one moment
    #: scope 4.2 designed it for.
    route: Route | None = None
    #: Distinct from `plan_id`: a job is a run, and a run can be resumed, retried or
    #: abandoned while the plan it is building keeps its identity.
    job_id: str | None = None
    profile: PreferenceProfile = PreferenceProfile()
    #: The costing model and avoid-areas every routing call in this plan carries, resolved
    #: once when the plan began. Persisted rather than recomputed, because a resume rebuilds
    #: the router in a different process and a policy recomputed there could silently differ
    #: from the one the first half of the route was drawn with.
    policy: RoutingPolicy = RoutingPolicy()
    segments: list[Segment] = Field(default_factory=list)
    results: list[ScorerResult] = Field(default_factory=list)
    etas: list[datetime] = Field(default_factory=list)
    locked: list[LockedRange] = Field(default_factory=list)
    trade_offs: list[TradeOff] = Field(default_factory=list)
    coverage: CoverageManifest = Field(default_factory=CoverageManifest)
    manifest: Manifest = Field(default_factory=Manifest)
    round: int = 0
    status: JobStatus = "running"
    #: What the loop stopped to ask, and what came back. `status` alone records that it
    #: stopped; across a process boundary that is not enough to resume.
    question: PendingQuestion | None = None
    answers: dict[str, str] = Field(default_factory=dict)
    #: Scope 6.3 caps preference questions at three *per plan*, and a plan outlives the
    #: process that started it - so the count is persisted or the cap is unenforceable
    #: across exactly the boundary it is meant to survive.
    questions_asked: int = 0

    def lock(
        self,
        start_m: float,
        end_m: float,
        reason: str | None = None,
        *,
        source: LockSource = "user",
    ) -> None:
        """Exclude a range from rerouting (scope 6.4).

        Called explicitly, and automatically when the user resolves a trade-off: a choice
        already made should not be silently reopened on the next iteration.

        `source` defaults to the runner for the reason `LockedRange.source` gives, so the
        one site that is the loop's own has to say so.

        **Appends, and deliberately does not merge.** Two overlapping locks and their union
        exclude exactly the same segments - `locked_segment_ids` is a union over ranges - so
        merging changes nothing the loop can see, and it *does* change something a reader
        can: the golden expectation records `loop.locked` verbatim, entry by entry, which is
        how it notices a lock that stopped being applied. Collapsing the list would move
        that without changing a measurement. `unlock` is where ranges are taken apart, and
        it handles overlaps because it has to.
        """
        self.locked.append(LockedRange(start_m=start_m, end_m=end_m, reason=reason, source=source))

    def unlock(
        self, start_m: float, end_m: float, *, source: LockSource | None = None
    ) -> list[LockedRange]:
        """Release a stretch of the line, and say what was holding it.

        Scope 10.3's "select a range -> lock/unlock" is one gesture with two halves and
        only one of them existed: `lock` appended, and nothing in the tree ever removed a
        `LockedRange` or narrowed one.

        **A partial overlap is trimmed, not dropped.** A runner who unlocks 2-3 km of a
        lock that spans 0-10 km has said nothing about the other nine, and removing the
        whole entry would silently reopen them to the next round's reroute - which is the
        failure scope 8.4's auto-lock exists to prevent, arriving through the undo button.
        Each surviving piece keeps the original `reason` and `source`, because they are
        still that lock: a trimmed range the loop wrote is still the loop's.

        `source` selects which author's locks may be taken apart - this is what M11.1's
        field is for. `None` means any, which is the honest default for a caller that said
        only "free this range"; the UI's "unlock what I locked" passes `"user"` and leaves
        the loop's own reroute locks standing.

        Returns the locks it removed or narrowed, **as they were before**, so a caller with
        a terminal can report what it released rather than reporting a count of nothing.
        """
        if end_m <= start_m:
            raise ValueError("unlocked range must have positive length")

        kept: list[LockedRange] = []
        freed: list[LockedRange] = []
        for lock in self.locked:
            selected = source is None or lock.source == source
            if not selected or lock.end_m <= start_m or lock.start_m >= end_m:
                kept.append(lock)
                continue
            freed.append(lock)
            # Up to two survivors: the head before the released range and the tail after
            # it. Both, for a lock the range falls strictly inside.
            if lock.start_m < start_m:
                kept.append(lock.model_copy(update={"end_m": start_m}))
            if lock.end_m > end_m:
                kept.append(lock.model_copy(update={"start_m": end_m}))
        self.locked = kept
        return freed

    def locked_ids(self) -> frozenset[str]:
        from longrun.core.geo.segments import locked_segment_ids

        return locked_segment_ids(self.segments, self.locked)

    def result(self, scorer: str) -> ScorerResult | None:
        return next((r for r in self.results if r.name == scorer), None)

    def put_result(self, result: ScorerResult) -> None:
        """Replace a scorer's previous output, so a rerun does not double-count flags."""
        self.results = [r for r in self.results if r.name != result.name]
        self.results.append(result)

    def save(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> Scratchpad:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def loads(cls, blob: str) -> Scratchpad:
        return cls.model_validate(json.loads(blob))
