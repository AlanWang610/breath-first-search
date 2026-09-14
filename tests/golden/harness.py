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
from longrun.core.plan.scratchpad import Scratchpad

ROUTES_DIR = Path(__file__).parent / "routes"

#: A port nothing listens on. A loop golden must replay its router answers, and pointing at
#: the default would let it succeed against whatever a developer happens to have running -
#: which is the difference between a hermetic test and one that passes on one machine.
UNREACHABLE_ROUTER = "http://127.0.0.1:9"

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

#: A generate-mode route has no `route.gpx`, and requiring one would be requiring the
#: answer: its geometry is what the loop produces, not what it is given. Everything else
#: is still required, including the cassette - the router answers are pinned exactly as
#: the forecast is.
GENERATED_FILES = tuple(f for f in REQUIRED_FILES if f != "route.gpx")


def required_files(directory: Path) -> tuple[str, ...]:
    """What this route must carry, which depends on how it is entered."""
    request = load_request(directory)
    return GENERATED_FILES if request.get("from") else REQUIRED_FILES


class GoldenError(RuntimeError):
    """A golden route could not be run at all, which is distinct from failing."""


@dataclass(frozen=True)
class GoldenRun:
    """One route, run end to end."""

    name: str
    directory: Path
    plan: Plan
    sheet: str
    #: Only a generate-mode run has one: `repair` scores a route once and never opens a
    #: scratchpad, which is exactly the difference this field records.
    scratchpad: Scratchpad | None = None


def route_names() -> list[str]:
    """Every golden route in the suite, in a stable order.

    Keyed on `request.yaml` rather than `route.gpx`: a generate-mode route has no GPX to
    be keyed on, and a discovery rule that required one would silently exclude the loop
    golden - which is the same class of failure as `test_the_golden_suite_is_not_empty`
    exists to prevent.
    """
    if not ROUTES_DIR.is_dir():
        return []
    return sorted(d.name for d in ROUTES_DIR.iterdir() if (d / "request.yaml").exists())


def load_request(directory: Path) -> dict[str, Any]:
    return dict(yaml.safe_load((directory / "request.yaml").read_text(encoding="utf-8")) or {})


def run(directory: Path, out_dir: Path) -> GoldenRun:
    """Invoke the CLI over a route directory and read back what it wrote.

    `repair` for a route directory that supplies its own geometry, and `plan` - the scope
    8.1 loop - for one whose `request.yaml` carries `from` and `to`. Both go through the
    CLI with `LONGRUN_OFFLINE=1` and **no model in the loop**, which is what makes the loop
    golden-testable at all (ADR 0015): every call site has a deterministic path, so the
    only thing a missing model changes is how terse a trade-off line reads.
    """
    request = load_request(directory)
    if request.get("from"):
        return _run_loop(directory, out_dir, request)
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

    return _read_back(directory, out_dir)


def _run_loop(directory: Path, out_dir: Path, request: dict[str, Any]) -> GoldenRun:
    """`longrun plan`, through the loop, replaying recorded router answers.

    The router is pointed at a port nothing is listening on. Every route, detour and match
    this needs is in the cassette, so a request that is *not* recorded fails as a loud
    `CacheMiss` rather than quietly reaching a GraphHopper somebody happens to be running.
    """
    args = [
        "plan",
        "--from",
        str(request["from"]),
        "--to",
        str(request["to"]),
        "--date",
        str(request["date"]),
        "--start",
        str(request["start"]),
        "--rounds",
        str(request.get("rounds", 5)),
        "--fixtures",
        str(directory / "fixtures"),
        "--profile",
        str(directory / "profile.yaml"),
        "--snapshot",
        str(directory / "snapshot.json"),
        "--cache",
        str(directory / "cache.sqlite"),
        "--router",
        UNREACHABLE_ROUTER,
        "--out",
        str(out_dir),
    ]
    if request.get("utc_offset_hours") is not None:
        args += ["--utc-offset", str(request["utc_offset_hours"])]

    result = CliRunner().invoke(app, args, env={"LONGRUN_OFFLINE": "1"})
    if result.exit_code != 0:
        raise GoldenError(
            f"`longrun plan` exited {result.exit_code} on {directory.name}:\n"
            f"{result.output}\n{result.exception!r}"
        )
    return _read_back(directory, out_dir)


def _read_back(directory: Path, out_dir: Path) -> GoldenRun:
    # The scratchpad is the loop's own state and the only place the round count and the
    # locks live - `Plan` carries what the loop *produced*, not what it did. A golden that
    # read only the plan could not tell one round from five.
    pad_path = out_dir / "scratchpad.json"
    scratchpad = Scratchpad.load(pad_path) if pad_path.exists() else None
    return GoldenRun(
        name=directory.name,
        directory=directory,
        plan=Plan.model_validate_json((out_dir / "plan.json").read_text(encoding="utf-8")),
        sheet=(out_dir / "sheet.md").read_text(encoding="utf-8"),
        scratchpad=scratchpad,
    )
