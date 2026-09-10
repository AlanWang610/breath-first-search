"""Running a golden route the way a user would (scope 10.1).

Through the CLI, with no model in the loop and nothing reaching the network. That is the
point of running goldens this way rather than calling the scorers directly: the thing
under test is the whole pipeline — GPX read, way matching, segmentation, scoring,
arbitration, verification — and a harness that assembled a `ScorerContext` itself would
be testing a path no user ever takes.

Everything the run depends on is pinned in the route directory and nothing is read from
the environment. `LONGRUN_OFFLINE=1` is exported for the invocation, so a scorer that
grows an external call fails the golden loudly instead of silently depending on the
network being up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.models.plan import Plan

ROUTES_DIR = Path(__file__).parent / "routes"

#: Every file a golden route directory must carry. `route.gpx` is the geometry,
#: `request.yaml` the pinned request, `profile.yaml` the preferences in force,
#: `snapshot.json` the source vintages, `cache.sqlite` the recorded forecast, and
#: `expected.json` the record of what the scorers used to say.
#:
#: The cassette is required, not optional. "This route was run against a recorded
#: forecast" is exactly the pin scope 6.4 asks for, and an optional file is one that
#: quietly stops existing.
REQUIRED_FILES = (
    "route.gpx",
    "request.yaml",
    "profile.yaml",
    "snapshot.json",
    "cache.sqlite",
    "expected.json",
)


class GoldenError(RuntimeError):
    """A golden route could not be run at all, which is distinct from failing."""


@dataclass(frozen=True)
class GoldenRun:
    """One route, run end to end."""

    name: str
    directory: Path
    plan: Plan
    sheet: str


def route_names() -> list[str]:
    """Every golden route in the suite, in a stable order."""
    if not ROUTES_DIR.is_dir():
        return []
    return sorted(d.name for d in ROUTES_DIR.iterdir() if (d / "route.gpx").exists())


def load_request(directory: Path) -> dict[str, Any]:
    return dict(yaml.safe_load((directory / "request.yaml").read_text(encoding="utf-8")) or {})


def run(directory: Path, out_dir: Path) -> GoldenRun:
    """Invoke `longrun repair` over a route directory and read back what it wrote."""
    request = load_request(directory)
    args = [
        "repair",
        str(directory / "route.gpx"),
        "--date",
        str(request["date"]),
        "--start",
        str(request["start"]),
        "--fixtures",
        str(directory / "fixtures"),
        "--profile",
        str(directory / "profile.yaml"),
        "--snapshot",
        str(directory / "snapshot.json"),
        "--cache",
        str(directory / "cache.sqlite"),
        "--out",
        str(out_dir),
    ]
    if request.get("target_km") is not None:
        args += ["--target-km", str(request["target_km"])]
    if request.get("utc_offset_hours") is not None:
        args += ["--utc-offset", str(request["utc_offset_hours"])]

    result = CliRunner().invoke(app, args, env={"LONGRUN_OFFLINE": "1"})
    if result.exit_code != 0:
        raise GoldenError(
            f"`longrun repair` exited {result.exit_code} on {directory.name}:\n"
            f"{result.output}\n{result.exception!r}"
        )

    return GoldenRun(
        name=directory.name,
        directory=directory,
        plan=Plan.model_validate_json((out_dir / "plan.json").read_text(encoding="utf-8")),
        sheet=(out_dir / "sheet.md").read_text(encoding="utf-8"),
    )
