"""Where a person's labels live before anything is published: `~/.longrun/labels/`.

Outside the repository on purpose (ADR 0048), for two reasons that point the same way:

* **The repository is public, and a label is about where somebody runs.** A verdict keeps a
  copy of the plan that was judged, and a plan starts at a door. The ledger holds all of it;
  `longrun label publish` writes into the tree only what is safe to publish.
* **The tree is where the agent works.** A ledger in the working copy is one `Write` away
  from any session; one in the runner's home is not in the path of anything the agent is
  asked to edit. That guards against an accident, not a determined process, and the ADR
  says so.

Beside `~/.longrun/profile.yaml` (`preferences.store.USER_PROFILE_PATH`), which is the same
kind of thing: evidence about one runner, kept where that runner keeps it.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

from longrun.core.labels.pairs import PairAnswer, PairTask, pair_from
from longrun.core.labels.pairs_io import dump_pairs
from longrun.core.labels.records import Verdict

LABELS_DIR_ENV_VAR = "LONGRUN_LABELS_DIR"
LABELER_ENV_VAR = "LONGRUN_LABELER"

#: Where the ledger lives when nothing says otherwise.
DEFAULT_LABELS_DIR = Path.home() / ".longrun" / "labels"


#: A task id is a file name in three directories, so it is held to characters that are one.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class AlreadyAnswered(ValueError):
    """A task keeps its first answer. A second look is a probe (`pairs.probe`), not an edit:
    an answer that could be overwritten would let a later mood quietly replace a judgement."""


def labels_dir_from_env(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    configured = source.get(LABELS_DIR_ENV_VAR, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_LABELS_DIR


def labeler_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """Who is labelling, or `None` when nobody has said.

    Never guessed from the OS account or the git author: a label under the wrong name is a
    label about the wrong runner, and `tune --labeler` would fit it to them.
    """
    source = os.environ if env is None else env
    return source.get(LABELER_ENV_VAR, "").strip() or None


class Ledger:
    """The on-disk ledger. One JSON file per record, named by the record's content."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def verdicts_dir(self) -> Path:
        return self.root / "verdicts"

    def record_verdict(self, verdict: Verdict, plan_json: str | None = None) -> Path:
        """Keep a verdict, and a copy of the plan it was written about.

        The plan copy is checked against `verdict.plan_sha256` rather than trusted: a label
        whose plan is not the plan it names is worse than one with no plan at all.
        """
        if plan_json is not None:
            digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
            if verdict.plan_sha256 != digest:
                raise ValueError(
                    f"the verdict names plan {verdict.plan_sha256} and was given {digest}"
                )
        self.verdicts_dir.mkdir(parents=True, exist_ok=True)
        path = self.verdicts_dir / f"{verdict.id}.json"
        path.write_text(verdict.model_dump_json(indent=2) + "\n", encoding="utf-8")
        if plan_json is not None:
            (self.verdicts_dir / f"{verdict.id}.plan.json").write_text(plan_json, encoding="utf-8")
        return path

    def verdicts(self) -> list[Verdict]:
        """Every verdict, oldest first."""
        if not self.verdicts_dir.is_dir():
            return []
        found = [
            Verdict.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.verdicts_dir.glob("*.json")
            if not path.name.endswith(".plan.json")
        ]
        return sorted(found, key=lambda v: (v.labelled_at, v.id))

    # --- preference pairs -------------------------------------------------------------

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def answers_dir(self) -> Path:
        return self.root / "answers"

    @property
    def pairs_dir(self) -> Path:
        """Tune-format YAML, one file per answered task: `longrun tune --pairs` reads it."""
        return self.root / "pairs"

    def add_task(self, task: PairTask) -> Path:
        _check_task_id(task.id)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        path = self.queue_dir / f"{task.id}.json"
        path.write_text(task.model_dump_json() + "\n", encoding="utf-8")
        return path

    def task(self, task_id: str) -> PairTask | None:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            return None
        path = self.queue_dir / f"{task_id}.json"
        return PairTask.model_validate_json(path.read_text("utf-8")) if path.is_file() else None

    def tasks(self) -> list[PairTask]:
        """Every task, oldest first."""
        if not self.queue_dir.is_dir():
            return []
        found = [
            PairTask.model_validate_json(path.read_text("utf-8"))
            for path in self.queue_dir.glob("*.json")
        ]
        return sorted(found, key=lambda t: (t.created_at, t.id))

    def answer(self, task_id: str) -> PairAnswer | None:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            return None
        path = self.answers_dir / f"{task_id}.json"
        return PairAnswer.model_validate_json(path.read_text("utf-8")) if path.is_file() else None

    def answers(self) -> list[PairAnswer]:
        if not self.answers_dir.is_dir():
            return []
        found = [
            PairAnswer.model_validate_json(path.read_text("utf-8"))
            for path in self.answers_dir.glob("*.json")
        ]
        return sorted(found, key=lambda a: (a.labelled_at, a.task_id))

    def record_answer(self, task: PairTask, answer: PairAnswer) -> PairAnswer:
        """Keep an answer, and the tune-format pair it is evidence for when it names a line."""
        _check_task_id(task.id)
        if answer.task_id != task.id or (answer.a_id, answer.b_id) != (task.a.id, task.b.id):
            raise ValueError(f"the answer is about {answer.task_id}, not the task it was given")
        path = self.answers_dir / f"{task.id}.json"
        if path.exists():
            raise AlreadyAnswered(f"task {task.id} was answered already")
        self.answers_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(answer.model_dump_json() + "\n", encoding="utf-8")
        pair = pair_from(task, answer)
        if pair is not None:
            self.pairs_dir.mkdir(parents=True, exist_ok=True)
            (self.pairs_dir / f"{task.id}.yaml").write_text(dump_pairs([pair]), encoding="utf-8")
        return answer


def _check_task_id(task_id: str) -> None:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"{task_id!r} is not a task id")


__all__ = [
    "TASK_ID_PATTERN",
    "AlreadyAnswered",
    "DEFAULT_LABELS_DIR",
    "LABELER_ENV_VAR",
    "LABELS_DIR_ENV_VAR",
    "Ledger",
    "labeler_from_env",
    "labels_dir_from_env",
]
