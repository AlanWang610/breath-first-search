"""What a person's label is, as a record the ledger keeps (ADR 0048).

`by` is fixed to `human` here, and that is the point of the type: the only labels this
module can represent are ones that came through a door a person uses. A `claude` or
`synthetic` label is still legal in `tests/eval` (ADR 0022), and is written by hand in the
tree where a reviewer reads it - never recorded through this path, and so never counted as
a person's by anything that reads the ledger.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

#: A labeler is an identifier: it names a published file and it is what `longrun tune
#: --labeler` fits on. Kept to characters that are safe in both, so a name never has to be
#: escaped on its way into a path.
LABELER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

Via = Literal["cli", "ui"]


class Verdict(BaseModel):
    """A "would you run this?" about one plan, with the numbers it was written against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What was judged: a golden route's name, or a stored plan's id.
    subject: str
    #: The golden this verdict can be re-checked against on every run of `tests/eval`. Only
    #: a verdict with one is published into the tree: one without is a statement about a
    #: plan whose numbers can never move again, which would raise the human count while
    #: testing nothing (ADR 0022's trap, in a new place).
    golden: str | None = None
    would_run: Literal["yes", "no", "unsure"]
    reason: str
    bounds: dict[str, float] = Field(default_factory=dict)
    #: What made a `no` that no measured number captures - a finding about the scorers,
    #: recorded rather than refused (ADR 0022, amended).
    gap: str | None = None
    note: str | None = None
    #: `bounds.measured_from_plan` of the plan as it was shown. Stored rather than recomputed
    #: because a stored plan is rewritten in place by every edit gesture, and what the person
    #: saw is the fact a label is about.
    measured: dict[str, float | int | None]
    #: sha256 of the plan JSON that was judged; the ledger keeps a copy under the same id.
    plan_sha256: str | None = None

    by: Literal["human"] = "human"
    labeler: str
    #: Passed in by the door that recorded it: `core/` reads no clock (scope 3.3). Aware,
    #: never naive: a naive time is a guess about which clock it was read off (ADR 0045),
    #: and the ledger orders verdicts by it - a naive one beside an aware one does not sort.
    labelled_at: AwareDatetime
    via: Via

    @field_validator("labeler")
    @classmethod
    def _labeler_is_an_identifier(cls, value: str) -> str:
        if not LABELER_PATTERN.fullmatch(value):
            raise ValueError(
                f"labeler {value!r} must be 1-40 letters, digits, '-' or '_', starting with a "
                "letter or digit"
            )
        return value

    @property
    def id(self) -> str:
        """A content address: the same verdict recorded twice is one record, not two."""
        canonical = self.model_dump_json()
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


__all__ = ["LABELER_PATTERN", "Verdict", "Via"]
