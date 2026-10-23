"""A fit becoming the vector a runner's plans are drawn with (ADR 0051).

ADR 0021 said a fitted vector "becomes the default for the runner it was fitted for" and
did not say who decides that it has. The runner does: `longrun tune --labeler NAME --save`
writes it to their own profile, and refuses unless the evidence clears every gate below.
A fit that passes is still not adopted by passing; the command is the decision, and it is a
person's (the same line `label publish` draws, for the same reason).

The gates, cheapest first, so the one that spends router calls runs last:

1. **One runner.** `--labeler` names them; a vector fitted to everybody is nobody's.
2. **Enough evidence.** `MIN_PAIRS` real pairs, as `fit` already demands.
3. **Something to say.** A neutral best vector is not saved: the profile already says that.
4. **No detours bought.** The fit's detour penalty is exactly zero.
5. **Better than the shorter line.** Held out, the 95% floor clears NEUTRAL (ADR 0021's
   amended bar).
6. **The router agrees.** Each pair re-drawn once (`reroute`, ADR 0020's deferred pass):
   nothing refused, more lines on the chosen side than the rejected one, none beyond the
   runner's detour tolerance.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from longrun.core.models.profile import FittedRouting, PreferenceEntry, Provenance
from longrun.core.routing.tuning import MIN_PAIRS

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.labels.reroute import RerouteReport
    from longrun.core.routing.tuning import Holdout, TuningResult


def refusals(
    result: TuningResult,
    *,
    labeler: str | None,
    holdout: Holdout | None,
    min_pairs: int = MIN_PAIRS,
) -> list[str]:
    """Why this fit may not be saved, from what is already measured; empty when it may.

    Gates 1-5. The sixth spends a routing call per pair and runs only once these pass.
    """
    out = []
    if labeler is None:
        out.append("a fit is saved for one runner (ADR 0021): name them with --labeler")
    if result.real_pairs < min_pairs:
        out.append(f"{result.real_pairs} pair(s) from a person; {min_pairs} are needed")
    elif result.params.is_neutral:
        out.append("the best vector on this evidence is neutral; there is nothing to save")
    if result.penalty > 0:
        out.append(
            f"the fit's detour penalty is {result.penalty:.3f}, not 0: some of its choices go "
            "beyond the detour tolerance"
        )
    if holdout is None:
        out.append("no held-out measurement to judge it by")
    elif not holdout.beats_neutral:
        out.append(
            f"held out, the fit's 95% floor ({holdout.interval[0]:.1%}) does not clear "
            f"neutral's {holdout.neutral:.1%}"
        )
    return out


def fitted_entry(
    result: TuningResult,
    *,
    labeler: str,
    holdout: Holdout,
    report: RerouteReport,
    today: date,
) -> PreferenceEntry[FittedRouting]:
    """The profile entry a fit that passed every gate becomes.

    `inferred`, like a pacing curve from history: derived from what the runner did, so
    anything they state about routing outranks it (scope 6.3).
    """
    low, high = holdout.interval
    return PreferenceEntry(
        value=FittedRouting(
            params=result.params,
            labeler=labeler,
            pairs=result.real_pairs,
            held_out=round(holdout.fitted, 4),
            held_out_interval=(round(low, 4), round(high, 4)),
            neutral_held_out=round(holdout.neutral, 4),
            rerouted=len(report.rows),
            followed_chosen=report.count("chosen"),
            followed_rejected=report.count("rejected"),
        ),
        provenance=Provenance.INFERRED,
        updated=today,
    )


__all__ = ["fitted_entry", "refusals"]
