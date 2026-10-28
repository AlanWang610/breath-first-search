"""Shape fixtures: a payload's shape, written from documentation (M18, ADR 0055).

Most sources cannot back a cassette - their payloads carry no redistribution grant (ADR
0038), and a 511 platform's terms often forbid republishing outright - and a parser with no
committed input is a parser nothing tests. A shape fixture is the answer ADR 0038 left open:
a payload **written by hand from the vendor's published documentation, with invented
values**, so it carries the field names, nesting and value formats a parser depends on and
none of the publisher's data.

Each file in `tests/contract/shapes/` is `<family>_<name>.json`:

    {
      "_provenance": {
        "family": "state511",
        "written_from": "https://511ga.org/developers/doc",   # the documentation, not the API
        "read_on": "2026-10-23",
        "values": "invented"
      },
      "payload": { ... }
    }

A hermetic test parses `payload`; a `network` test beside it asserts that the live
payload's keys include the fixture's, so a fixture that drifts from the real shape fails
somewhere rather than nowhere. What a shape fixture cannot catch is a value the
documentation got wrong - which is why every documented format (epoch units, offsets) is
written into it rather than left to a default.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

SHAPES = Path(__file__).parent / "shapes"
CASSETTES = Path(__file__).parent / "cassettes"


def provenance_problems(document: Any) -> list[str]:
    """What is wrong with one shape fixture's envelope, or nothing."""
    if not isinstance(document, dict):
        return ["is not a JSON object"]
    problems: list[str] = []
    meta = document.get("_provenance")
    if not isinstance(meta, dict):
        return ["has no _provenance block"]
    if not str(meta.get("written_from", "")).startswith("https://"):
        problems.append("does not say which documentation it was written from (https)")
    try:
        date.fromisoformat(str(meta.get("read_on")))
    except ValueError:
        problems.append("does not say when the documentation was read (ISO date)")
    if meta.get("values") != "invented":
        problems.append("does not declare its values invented")
    if not meta.get("family"):
        problems.append("names no family")
    if "payload" not in document:
        problems.append("has no payload")
    return problems


def test_the_shapes_directory_exists_and_explains_itself() -> None:
    assert (SHAPES / "README.md").exists()


@pytest.mark.parametrize(
    "path", sorted(SHAPES.glob("*.json")), ids=lambda p: p.name if isinstance(p, Path) else str(p)
)
def test_every_shape_fixture_says_it_was_written_not_recorded(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert provenance_problems(document) == [], path.name
    family = document["_provenance"]["family"]
    assert path.name.startswith(f"{family}_"), f"{path.name} is filed under the wrong family"
    assert not (CASSETTES / path.name).exists(), f"{path.name} is both recorded and written"


def test_a_fixture_that_cannot_say_where_it_came_from_is_refused() -> None:
    good = {
        "_provenance": {
            "family": "state511",
            "written_from": "https://511ga.org/developers/doc",
            "read_on": "2026-10-23",
            "values": "invented",
        },
        "payload": [],
    }
    assert provenance_problems(good) == []
    recorded = {**good, "_provenance": {**good["_provenance"], "values": "recorded"}}
    assert provenance_problems(recorded) == ["does not declare its values invented"]
    assert provenance_problems({"payload": []}) == ["has no _provenance block"]
    undated = {**good, "_provenance": {**good["_provenance"], "read_on": "last week"}}
    assert provenance_problems(undated) == [
        "does not say when the documentation was read (ISO date)"
    ]
