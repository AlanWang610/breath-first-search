"""What of a person's ledger may enter the tree, and in what form (ADR 0048).

Pure: which verdict speaks for a golden, whether it was written against the numbers that
golden's `expected.json` holds now, and the `label.yaml` it becomes. `longrun label
publish` does the reading and writing, and the person reviews the diff before committing,
the same review `--update-golden` gets.
"""

from __future__ import annotations

from collections.abc import Iterable

import yaml

from longrun.core.labels.bounds import Measured
from longrun.core.labels.records import Verdict

#: How far a judged number may sit from the golden's and still be the same number. The
#: digest rounds summary values to three places; anything wider is a different plan.
MATCH_TOLERANCE = 1e-3

_HEADER = """\
# Whether a runner would run this route, and what in the numbers says so.
#
# Written by `longrun label publish` from the labeler's own ledger (ADR 0048). A person
# judged this route; to change the label, judge it again and publish, rather than editing
# this file - a hand edit here is a label nobody gave.
#
# `would_run` is quoted because YAML 1.1 reads a bare yes/no as a boolean.

"""


def latest_by_golden(verdicts: Iterable[Verdict]) -> dict[str, Verdict]:
    """The most recent verdict about each golden. A later judgement replaces an earlier one
    by the same person, the way a runner's second look at a route supersedes their first."""
    out: dict[str, Verdict] = {}
    for verdict in sorted(verdicts, key=lambda v: (v.labelled_at, v.id)):
        if verdict.golden is not None:
            out[verdict.golden] = verdict
    return out


def differences(judged: Measured, current: Measured) -> list[str]:
    """Where the numbers a person judged are not the numbers the suite will check against.

    Non-empty means the verdict was about a different plan - one edited after it was seeded
    from the golden, or a golden that has since moved - and publishing it would pin a
    judgement to a route the person never saw.
    """
    out: list[str] = []
    for metric in sorted(set(judged) | set(current)):
        was, now = judged.get(metric), current.get(metric)
        if was is None or now is None:
            if was != now:
                out.append(f"{metric}: judged {was}, golden {now}")
        elif abs(float(was) - float(now)) > MATCH_TOLERANCE:
            out.append(f"{metric}: judged {was}, golden {now}")
    return out


def case_yaml(verdict: Verdict) -> str:
    """The `tests/eval/cases/<golden>/label.yaml` a verdict becomes.

    Carries the date and not the time: when a person judged a route is worth keeping, the
    minute they did it is not something a public repository needs to know about them.
    """
    if verdict.golden is None:
        raise ValueError("only a verdict about a golden is published (ADR 0048)")
    body: dict[str, object] = {
        "golden": verdict.golden,
        "would_run": verdict.would_run,
        "by": verdict.by,
        "labeler": verdict.labeler,
        "labelled_on": verdict.labelled_at.date().isoformat(),
        "reason": verdict.reason,
    }
    if verdict.bounds:
        body["bounds"] = dict(verdict.bounds)
    if verdict.gap is not None:
        body["gap"] = verdict.gap
    if verdict.note is not None:
        body["note"] = verdict.note
    return _HEADER + yaml.safe_dump(body, sort_keys=False, width=88, allow_unicode=True)


__all__ = ["MATCH_TOLERANCE", "case_yaml", "differences", "latest_by_golden"]
