"""Preference pairs on disk: the YAML `longrun tune` reads (ADR 0020).

Moved out of `cli/tune.py` in M23.1, when a second writer arrived: `longrun label` records
a pair a person judged, and `tune --edit` records one a person drew. Two writers of one
format is one format only while they share the code that writes it.

A pair on disk carries route *summaries* - metres per way class - and never geometry. ADR
0020 chose that so a pair survives an OSM vintage change; it is also what makes a pair
publishable from a public repository when the routes it compares start at somebody's door.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from longrun.core.routing.tuning import PreferencePair, RouteSummary

ClassKey = tuple[int | None, bool, bool, bool]


def class_key(key: ClassKey) -> str:
    """A way class as a YAML-safe string. The inverse of `summary_from_dict`'s split."""
    lts, unpaved, is_path, is_collector = key
    flags = "|".join("true" if flag else "false" for flag in (unpaved, is_path, is_collector))
    return f"{'none' if lts is None else lts}|{flags}"


def summary_from_dict(data: dict[str, Any]) -> RouteSummary:
    """A stored summary back into a `RouteSummary`.

    The class key is a four-part string on disk rather than a tuple, because YAML has no
    tuple and a list key is not hashable. `"3|false|false|true"` is LTS 3, paved, not a
    path, a collector - and `"none"` is an LTS nobody could determine, which must survive
    the round trip as `None` rather than becoming the string.
    """
    buckets: dict[ClassKey, float] = {}
    for key, metres in (data.get("metres_by_class") or {}).items():
        lts_text, unpaved, is_path, is_collector = str(key).split("|")
        buckets[
            (
                None if lts_text == "none" else int(lts_text),
                unpaved == "true",
                is_path == "true",
                is_collector == "true",
            )
        ] = float(metres)
    return RouteSummary(
        name=str(data.get("name", "route")),
        length_m=float(data.get("length_m", sum(buckets.values()))),
        metres_by_class=buckets,
    )


def summary_to_dict(summary: RouteSummary) -> dict[str, Any]:
    return {
        "name": summary.name,
        "length_m": round(summary.length_m, 1),
        "metres_by_class": {
            class_key(key): round(metres, 1) for key, metres in summary.metres_by_class.items()
        },
    }


def pair_to_dict(pair: PreferencePair) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": pair.source,
        "preferred": pair.preferred,
        "weight": pair.weight,
        "a": summary_to_dict(pair.a),
        "b": summary_to_dict(pair.b),
    }
    if pair.labeler is not None:
        out["labeler"] = pair.labeler
    return out


def dump_pairs(pairs: Iterable[PreferencePair]) -> str:
    """The on-disk form of a pair file."""
    return yaml.safe_dump({"pairs": [pair_to_dict(pair) for pair in pairs]}, sort_keys=True)


def read_pairs(directory: Path) -> list[PreferencePair]:
    """Every pair in every `*.yaml` directly under `directory`; none if it does not exist.

    An entry with no `source` is `synthetic`, the one value `fit` never counts: a pair
    that does not say where it came from is not evidence about anybody (ADR 0022).
    """
    pairs: list[PreferencePair] = []
    if not directory.is_dir():
        return pairs
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data.get("pairs", []):
            labeler = entry.get("labeler")
            pairs.append(
                PreferencePair(
                    a=summary_from_dict(entry["a"]),
                    b=summary_from_dict(entry["b"]),
                    preferred=str(entry["preferred"]),
                    source=str(entry.get("source", "synthetic")),
                    weight=float(entry.get("weight", 1.0)),
                    labeler=None if labeler is None else str(labeler),
                )
            )
    return pairs


__all__ = [
    "ClassKey",
    "class_key",
    "dump_pairs",
    "pair_to_dict",
    "read_pairs",
    "summary_from_dict",
    "summary_to_dict",
]
