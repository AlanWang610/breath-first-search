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
from collections.abc import Mapping
from pathlib import Path

from longrun.core.labels.records import Verdict

LABELS_DIR_ENV_VAR = "LONGRUN_LABELS_DIR"
LABELER_ENV_VAR = "LONGRUN_LABELER"

#: Where the ledger lives when nothing says otherwise.
DEFAULT_LABELS_DIR = Path.home() / ".longrun" / "labels"


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


__all__ = [
    "DEFAULT_LABELS_DIR",
    "LABELER_ENV_VAR",
    "LABELS_DIR_ENV_VAR",
    "Ledger",
    "labeler_from_env",
    "labels_dir_from_env",
]
