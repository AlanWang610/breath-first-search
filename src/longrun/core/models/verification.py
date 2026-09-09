"""What `gpx_verify` returns (scope 7.9, 8.1).

These live in `core.models` rather than beside the checks that produce them because a
`Plan` carries its verification: scope 8.1 step 9 sends a failing check back to rerouting
with the segment it named, and scope 9 renders the checklist into the plan sheet. A plan
that has been verified but cannot say so is a plan whose sheet has to be re-derived to
learn what it already knew.

The three-state result is the load-bearing part. A check that could not run reports
`skipped`, never `passed` — reporting a pass because an input was missing is exactly the
silent false assurance scope 3.6 exists to prevent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["passed", "failed", "skipped"]


class CheckResult(BaseModel):
    """One numbered check's outcome."""

    number: int
    name: str
    status: CheckStatus
    offenders: list[str] = Field(default_factory=list)
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def blocking(self) -> bool:
        """Only an outright failure blocks; a skip is reported, not fatal."""
        return self.status == "failed"


class VerifyReport(BaseModel):
    """The outcome of all ten checks."""

    results: list[CheckResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when nothing failed. Skips do not fail a plan, but are reported."""
        return not any(r.blocking for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def skipped(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "skipped"]

    def offending_segments(self) -> list[str]:
        """Everything a failure named, for scope 8.1 step 9 to reroute."""
        return [o for r in self.failures for o in r.offenders]

    def summary(self) -> str:
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for result in self.results:
            counts[result.status] += 1
        return (
            f"{counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['skipped']} skipped of {len(self.results)}"
        )


__all__ = ["CheckResult", "CheckStatus", "VerifyReport"]
