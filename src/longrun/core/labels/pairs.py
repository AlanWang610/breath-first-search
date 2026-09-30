"""A preference pair: two lines the router drew under competing vectors (ADR 0049).

A person is shown two lines between the same ends and asked which they would rather run.
Both lines are the router's - drawn under different priority vectors, or offered as its own
alternatives - because a pair is only evidence about the six parameters if the lines differ
in the ways those parameters can see (ADR 0020). A line somebody drew by hand is `tune
--edit`'s business.

Three rules shape a task, and each one closes a way the answer could stop being the
person's:

* **Blind.** Which vector drew which line is kept in the ledger and never shown, and A and
  B are fixed when the task is made rather than by the vector that happens to be first.
* **Numbers after.** A task carries each line's shares of LTS 3+, unpaved and path, and a
  door shows them only once the person has answered. Shown first, the choice would follow
  the project's own LTS reading back into the fit - the circularity docs/tuning.md warns
  about.
* **No preference is not a pair.** It is kept and counted - the rate is itself evidence
  about whether the parameters matter where somebody runs - but a `PreferencePair` has no
  way to say "neither", and inventing one would put a label in the fit nobody gave.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from longrun.core.labels.pairs_io import class_key, summary_from_dict
from longrun.core.labels.records import LABELER_PATTERN, Via
from longrun.core.models.geometry import Route
from longrun.core.routing.custom_model import PARAMETER_NAMES, CustomModelParams
from longrun.core.routing.tuning import PreferencePair, RouteSummary, cost

Prefer = Literal["a", "b", "none", "cant"]
Strength = Literal["slight", "clear"]

#: Below this share of `a` that `b` also runs, two lines are different routes. Above it they
#: are one route with a wiggle, and nobody can prefer a wiggle on purpose.
MAX_SHARED_FRACTION = 0.85

#: And they have to differ in what they are made of, not only in where they go: two lines
#: through different streets of one class are the same pair to the fitter, which sees
#: nothing but metres per class.
MIN_CLASS_DIFFERENCE_M = 150.0

#: Levels for `split_fraction`'s grid: 3**6 = 729 vectors, around indifference. Coarser than
#: `tuning.DEFAULT_GRID` on purpose - this ranks questions, it does not fit an answer.
SPLIT_GRID: tuple[float, ...] = (0.5, 1.0, 2.0)

#: What an answer's strength is worth to `tuning.fit`.
WEIGHTS: dict[str, float] = {"slight": 0.5, "clear": 1.0}


class Candidate(BaseModel):
    """One line a person may be shown, and - in the ledger only - where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    #: The vector's name ("neutral", "seek-paths", ...) or "neutral-alt2". Never shown.
    drawn_by: str
    points: list[tuple[float, float]] = Field(description="(lat, lon), the router's line.")
    length_m: float
    metres_by_class: dict[str, float]

    @classmethod
    def of(cls, candidate_id: str, drawn_by: str, route: Route, summary: RouteSummary) -> Candidate:
        return cls(
            id=candidate_id,
            drawn_by=drawn_by,
            points=[(round(p.lat, 6), round(p.lon, 6)) for p in route.points],
            length_m=round(route.length_m, 1),
            metres_by_class={
                class_key(key): round(m, 1) for key, m in summary.metres_by_class.items()
            },
        )

    def summary(self) -> RouteSummary:
        """Named by id and nothing else: the name is what a published pair file carries."""
        return summary_from_dict(
            {"name": self.id, "length_m": self.length_m, "metres_by_class": self.metres_by_class}
        )

    def shares(self) -> dict[str, float]:
        """What a door may show once an answer is in: shares of LTS 3+, unpaved and path."""
        total = sum(self.metres_by_class.values()) or 1.0
        lts3_plus = unpaved = path = 0.0
        for key, metres in self.metres_by_class.items():
            lts, is_unpaved, is_path, _ = key.split("|")
            lts3_plus += metres if lts in ("3", "4") else 0.0
            unpaved += metres if is_unpaved == "true" else 0.0
            path += metres if is_path == "true" else 0.0
        return {
            "lts3_plus": round(lts3_plus / total, 3),
            "unpaved": round(unpaved / total, 3),
            "path": round(path / total, 3),
        }


class PairTask(BaseModel):
    """One question: these two lines, which would you rather run?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    #: Where the ends are and which graph drew the lines. Ledger only; never published.
    region: str | None
    ends: tuple[tuple[float, float], tuple[float, float]]
    a: Candidate
    b: Candidate
    #: Which of the six parameters the two lines differ on most, for ordering.
    stratum: str
    #: Share of `SPLIT_GRID`'s vectors that prefer `a`: near 0.5 is a question the grid
    #: cannot answer without a person.
    split: float
    #: A test-retest probe re-asks an earlier task with A and B swapped.
    repeat_of: str | None = None
    created_at: AwareDatetime


class PairAnswer(BaseModel):
    """What a person said about one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    prefer: Prefer
    strength: Strength | None = None
    reason: str | None = None
    #: The candidate ids behind A and B, so an answer survives a task being re-rendered.
    a_id: str
    b_id: str
    by: Literal["human"] = "human"
    labeler: str
    labelled_at: AwareDatetime
    via: Via

    @field_validator("labeler")
    @classmethod
    def _labeler_is_an_identifier(cls, value: str) -> str:
        if not LABELER_PATTERN.fullmatch(value):
            raise ValueError(f"labeler {value!r} must be 1-40 letters, digits, '-' or '_'")
        return value


def answer_problems(prefer: str, strength: str | None) -> list[str]:
    problems: list[str] = []
    if prefer not in ("a", "b", "none", "cant"):
        problems.append(f"prefer must be a, b, none or cant, not {prefer!r}")
    elif prefer in ("a", "b") and strength not in WEIGHTS:
        problems.append("say how strongly: slight or clear")
    elif prefer in ("none", "cant") and strength is not None:
        problems.append("a strength goes with a choice, not with 'none' or 'cant'")
    return problems


def pair_from(task: PairTask, answer: PairAnswer) -> PreferencePair | None:
    """The `PreferencePair` an answer is evidence for, or `None` when it names no line."""
    if answer.prefer not in ("a", "b") or answer.strength is None:
        return None
    return PreferencePair(
        a=task.a.summary(),
        b=task.b.summary(),
        preferred=answer.prefer,
        source="judged",
        weight=WEIGHTS[answer.strength],
        labeler=answer.labeler,
    )


# --- which pairs are worth asking ------------------------------------------------


def class_difference_m(a: RouteSummary, b: RouteSummary) -> float:
    """Metres that would have to move between classes to turn one summary into the other."""
    keys = set(a.metres_by_class) | set(b.metres_by_class)
    return sum(abs(a.metres_by_class.get(k, 0.0) - b.metres_by_class.get(k, 0.0)) for k in keys) / 2


def distinct(shared_fraction: float, a: RouteSummary, b: RouteSummary) -> bool:
    """Two lines a person could prefer on purpose, and a fitter could learn from."""
    return (
        shared_fraction <= MAX_SHARED_FRACTION
        and class_difference_m(a, b) >= MIN_CLASS_DIFFERENCE_M
    )


def _metres(summary: RouteSummary, parameter: str) -> float:
    def matches(key: tuple[int | None, bool, bool, bool]) -> bool:
        lts, unpaved, is_path, is_collector = key
        return {
            "lts2": lts == 2,
            "lts3": lts == 3,
            "lts4": lts == 4,
            "unpaved": unpaved,
            "path_bonus": is_path,
            "missing_sidewalk": is_collector,
        }[parameter]

    return sum(m for key, m in summary.metres_by_class.items() if matches(key))


def stratum(a: RouteSummary, b: RouteSummary) -> str:
    """Which of the six parameters the two lines differ on most, in metres."""
    return max(PARAMETER_NAMES, key=lambda name: abs(_metres(a, name) - _metres(b, name)))


def split_fraction(a: RouteSummary, b: RouteSummary, grid: Sequence[float] = SPLIT_GRID) -> float:
    """Share of the grid's vectors that cost `a` lower than `b`, ties counting half.

    A pair every plausible vector agrees on teaches the fit nothing it did not assume; one
    the grid splits on is where a person's answer moves it. Cheap, and not active learning:
    for the few dozen pairs one person will answer, ordering by this is enough.
    """
    votes = 0.0
    vectors = list(itertools.product(grid, repeat=len(PARAMETER_NAMES)))
    for values in vectors:
        params = CustomModelParams.from_vector(values)
        cost_a, cost_b = cost(a, params), cost(b, params)
        votes += 1.0 if cost_a < cost_b else 0.5 if cost_a == cost_b else 0.0
    return votes / len(vectors)


def order(tasks: Iterable[PairTask]) -> list[PairTask]:
    """Round-robin across strata, and within one the question the grid splits on most first.

    Stratified so a session that stops early has asked about more than one parameter; a
    queue sorted by split alone spends the first twenty answers on whichever class happens
    to separate lines most often where the person runs.

    **Probes go last.** A probe has its original's distance from 0.5, so by split alone it
    would be asked straight after it - and a person who has just answered a question
    answers its mirror image from memory, which tests recall rather than judgement.
    """
    materialised = list(tasks)
    by_stratum: dict[str, list[PairTask]] = {}
    originals = [t for t in materialised if t.repeat_of is None]
    for task in sorted(originals, key=lambda t: (abs(t.split - 0.5), t.id)):
        by_stratum.setdefault(task.stratum, []).append(task)
    queues = [by_stratum[name] for name in sorted(by_stratum)]
    out: list[PairTask] = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    probes = [t for t in materialised if t.repeat_of is not None]
    return out + sorted(probes, key=lambda t: (t.created_at, t.id))


def probe(task: PairTask, probe_id: str, created_at: datetime) -> PairTask:
    """The same question with A and B swapped - a person asked twice should answer once."""
    return task.model_copy(
        update={
            "id": probe_id,
            "a": task.b,
            "b": task.a,
            "split": round(1.0 - task.split, 4),
            "repeat_of": task.id,
            "created_at": created_at,
        }
    )


__all__ = [
    "MAX_SHARED_FRACTION",
    "MIN_CLASS_DIFFERENCE_M",
    "SPLIT_GRID",
    "WEIGHTS",
    "Candidate",
    "PairAnswer",
    "PairTask",
    "Prefer",
    "Strength",
    "answer_problems",
    "class_difference_m",
    "distinct",
    "order",
    "pair_from",
    "probe",
    "split_fraction",
    "stratum",
]
