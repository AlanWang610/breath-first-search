"""A person's judgements about routes, and the rules that keep them a person's (ADR 0048).

Two kinds of label reach this project from somebody who runs:

* a **verdict** - "would you run this?" about one plan, checked by `tests/eval` against the
  numbers it was written against (ADR 0022);
* a **preference** - which of two lines they would rather run, which `longrun tune` fits the
  six scope 7.1 parameters to (ADR 0020, 0021).

Both are recorded in a ledger outside the repository (`~/.longrun/labels/`) and reach the
tree only through `longrun label publish`. Nothing here is reachable from `tools/`, which is
the agent's door: ADR 0022 exists to stop an agent manufacturing either kind of label, and a
label tool on the MCP surface would be exactly that door.
"""
