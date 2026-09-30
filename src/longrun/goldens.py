"""How a golden route directory is run: one argv, for every reader that needs one.

`tests/golden/harness.py` runs goldens to pin what the scorers say, and `longrun label show`
runs one so a person can judge it (ADR 0048). A verdict is re-checked against the golden's
`expected.json` on every run of `tests/eval`, so the plan the person judged has to be the
plan that file was written from - produced by the same command with the same pinned inputs.
Two copies of this argv would be two chances for that to stop being true, and the copy that
drifted would be the one with no test over it. (`ui/gestures/seed.ts` keeps a third, in
TypeScript, and cites this file.)

Everything is pinned in the route directory. The caller runs the command with
`LONGRUN_OFFLINE=1`, and a loop golden's router is a port nothing listens on, so a request
that was not recorded fails as a loud `CacheMiss` rather than quietly reaching a GraphHopper
somebody happens to be running.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: Where the goldens live, relative to the repository root, which is where every command
#: that reads them runs from - the same convention as `tune`'s `tests/eval/pairs`.
DEFAULT_GOLDENS_DIR = Path("tests/golden/routes")

#: A port nothing listens on. A loop golden must replay its router answers, and pointing at
#: the default would let it succeed against whatever a developer happens to have running.
UNREACHABLE_ROUTER = "http://127.0.0.1:9"


def load_request(directory: Path) -> dict[str, Any]:
    return dict(yaml.safe_load((directory / "request.yaml").read_text(encoding="utf-8")) or {})


def is_golden(directory: Path) -> bool:
    """Keyed on `request.yaml`, not `route.gpx`: a generate-mode golden has no GPX."""
    return (directory / "request.yaml").is_file()


def golden_args(directory: Path, out_dir: Path) -> list[str]:
    """The `longrun` argv that runs one golden into `out_dir`.

    `repair` for a directory that supplies its own geometry, and `plan` - the scope 8.1
    loop - for one whose `request.yaml` carries `from` and `to`.
    """
    request = load_request(directory)
    pinned = [
        "--date",
        str(request["date"]),
        "--start",
        str(request["start"]),
    ]
    if request.get("from"):
        args = [
            "plan",
            "--from",
            str(request["from"]),
            "--to",
            str(request["to"]),
            *pinned,
            "--rounds",
            str(request.get("rounds", 5)),
        ]
    else:
        args = ["repair", str(directory / "route.gpx"), *pinned]
    args += [
        "--fixtures",
        str(directory / "fixtures"),
        "--profile",
        str(directory / "profile.yaml"),
        "--snapshot",
        str(directory / "snapshot.json"),
        "--cache",
        str(directory / "cache.sqlite"),
    ]
    if request.get("from"):
        args += ["--router", UNREACHABLE_ROUTER]
    args += ["--out", str(out_dir)]
    if not request.get("from") and request.get("target_km") is not None:
        args += ["--target-km", str(request["target_km"])]
    if request.get("utc_offset_hours") is not None:
        args += ["--utc-offset", str(request["utc_offset_hours"])]
    return args


__all__ = [
    "DEFAULT_GOLDENS_DIR",
    "UNREACHABLE_ROUTER",
    "golden_args",
    "is_golden",
    "load_request",
]
