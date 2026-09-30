# Working in this repository

Read `README.md` first: its "Ground rules these boundaries enforce" section is binding.

## Labels are a person's, never the agent's (ADR 0022, ADR 0048)

Human labels - eval verdicts and preference pairs - are the one input this project cannot
generate for itself. An agent must never:

- run `longrun label verdict`, `longrun label pair` or any other `label` command that records
  an answer, or call a `/api/.../verdict` or `/api/labels/...` write endpoint;
- write or edit anything under `~/.longrun/labels/` (or `LONGRUN_LABELS_DIR`);
- write `by: human` into `tests/eval/`, or add pairs to `tests/eval/pairs/`;
- run `longrun label publish` - the person runs it and reviews the diff.

Building, testing and fixing the label machinery is fine; tests use a temporary ledger
(`tests/conftest.py::_labels_stay_out_of_home`). `tools/` (the MCP surface) must never gain a
label tool. If a task seems to need a label, stop and ask the person for it.
