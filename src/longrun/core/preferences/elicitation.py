"""Asking about a preference, in context and almost never (scope 6.3).

> No up-front questionnaire. A preference question is permitted only when two candidate
> alternatives differ mainly on one profile axis and that axis is still `default`; the
> question is then concrete (which option, what differs, at what mile) and the answer is
> stored as `stated`. Cap 3 questions per plan.

The reason is stated in the scope and worth repeating, because it is what makes the rule
worth the machinery: *"questionnaire answers to situations the user hasn't faced would be
stored as `stated` with the same authority as real ones; in-context elicitation keeps
`stated` meaningful."*

**The hard part is not the cap.** `arbitrate` can say two candidates differ in the COMFORT
tier by 0.04, and cannot say *which preference* caused it - flags carry a scorer and a
severity, and nothing maps back. `SCORER_AXIS` is that inversion, and it is deliberately
small and explicit: a mapping that guessed would produce a question about the wrong thing,
which is worse than not asking.

Nothing here decides the phrasing. `agent/questions.py` writes the words, from a model when
there is one and a template when there is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.core.models.profile import PreferenceProfile
    from longrun.core.plan.arbitrate import Candidate

#: Scope 6.3: "cap 3 questions per plan". Per *plan*, which outlives the process that
#: started it - so the count lives on the scratchpad, not in a loop variable.
MAX_QUESTIONS_PER_PLAN = 3

#: Which preference a scorer's flags are about. The inversion `arbitrate` cannot do.
#:
#: Deliberately partial: a scorer absent from this table is one whose flags no single
#: preference explains, and asking about it would be asking the wrong question. `legality`
#: and `closures` are absent because they are not preferences at all - a closed road is not
#: a matter of taste, and scope 6.3 keeps safety floors outside the profile entirely.
SCORER_AXIS: dict[str, str] = {
    "segment_hostility": "traffic_tolerance",
    "crossings": "traffic_tolerance",
    "surface_profile": "surface",
    "sun_exposure": "sun",
    "stop_density": "stops_tolerance",
    "services_along": "water_gap_max_min",
    "resupply_schedule": "water_gap_max_min",
    "lighting": "darkness_tolerance",
}

#: Scope 6.3's two exceptions, "asked at first use because they affect nearly every plan".
#: `defaults.yaml` annotates both as such, which is where the list came from.
FIRST_USE_AXES = ("traffic_tolerance", "carry_capacity_ml")

#: How much of the difference between two candidates must come from one axis before the
#: difference is "mainly" about it. Below this the routes differ for several reasons at
#: once and a question about any one of them would misdescribe the choice.
DOMINANT_SHARE = 0.6


@dataclass(frozen=True)
class ElicitationQuestion:
    """One question worth asking, before anybody has phrased it."""

    axis: str
    options: list[str]
    at_km: float
    detail: str

    @property
    def kind(self) -> str:
        return "preference"


def is_default(profile: PreferenceProfile, axis: str) -> bool:
    """Whether nobody has ever said anything about this axis.

    Cheap, and the load-bearing half of scope 6.3's condition: an axis a runner has already
    stated or that history has inferred is one they should not be asked about again.
    """
    from longrun.core.models.profile import Provenance

    entry = getattr(profile, axis, None)
    return entry is not None and entry.provenance is Provenance.DEFAULT


def dominant_axis(a: Candidate, b: Candidate) -> tuple[str, float, str] | None:
    """The axis two candidates mainly differ on, with its share and a description.

    Returns `None` when no single axis accounts for `DOMINANT_SHARE` of the difference -
    which is the common case, and the honest answer: two routes usually differ for several
    reasons and a question about one of them would misdescribe the choice.
    """
    by_axis: dict[str, float] = {}
    for candidate, sign in ((a, 1.0), (b, -1.0)):
        for ranked in candidate.ranked:
            axis = SCORER_AXIS.get(ranked.flag.scorer)
            if axis is None:
                continue
            by_axis[axis] = by_axis.get(axis, 0.0) + sign * ranked.weighted

    total = sum(abs(value) for value in by_axis.values())
    if total <= 0:
        return None

    axis, difference = max(by_axis.items(), key=lambda item: abs(item[1]))
    share = abs(difference) / total
    if share < DOMINANT_SHARE:
        return None

    better, worse = (b.label, a.label) if difference > 0 else (a.label, b.label)
    return axis, share, f"{better} is better on {axis.replace('_', ' ')} than {worse}"


def question_for(
    a: Candidate,
    b: Candidate,
    profile: PreferenceProfile,
    *,
    asked: int,
    at_km: float,
) -> ElicitationQuestion | None:
    """Scope 6.3's whole condition, in one place.

    Four things must all hold, and the order is cheapest-first rather than
    most-important-first, because the cap is the one that stops most of them.
    """
    if asked >= MAX_QUESTIONS_PER_PLAN:
        return None

    found = dominant_axis(a, b)
    if found is None:
        return None
    axis, _, detail = found

    if not is_default(profile, axis):
        return None

    return ElicitationQuestion(axis=axis, options=[a.label, b.label], at_km=at_km, detail=detail)


def first_use_questions(profile: PreferenceProfile) -> list[str]:
    """The two axes scope 6.3 allows asking about up front, and only those two.

    "Because they affect nearly every plan" - and the exception is narrow on purpose: a
    third would be a questionnaire, which is the thing the rule exists to prevent.
    """
    return [axis for axis in FIRST_USE_AXES if is_default(profile, axis)]


def record_answer(
    profile: PreferenceProfile, axis: str, value: object, *, on: date
) -> tuple[PreferenceProfile, list[str]]:
    """Store an answer as `stated`, through `merge` (scope 6.3).

    Through `merge` rather than `apply_overrides`: `merge` is the only path that checks
    `supersedes` and reports what it refused, and an override's bare form deliberately
    preserves the existing provenance - which would file a runner's own answer as a
    default. `_check_floors` runs on that path too, so an answer that breaches a floor is
    refused rather than clamped.
    """
    from longrun.core.models.profile import PreferenceEntry, Provenance
    from longrun.core.preferences.store import merge

    entry = PreferenceEntry(value=value, provenance=Provenance.STATED, updated=on)
    return merge(profile, profile.model_copy(update={axis: entry}))


__all__ = [
    "DOMINANT_SHARE",
    "FIRST_USE_AXES",
    "MAX_QUESTIONS_PER_PLAN",
    "SCORER_AXIS",
    "ElicitationQuestion",
    "dominant_axis",
    "first_use_questions",
    "is_default",
    "question_for",
    "record_answer",
]
