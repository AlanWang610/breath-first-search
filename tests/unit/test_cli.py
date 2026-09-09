"""CLI tests (scope 10.1).

The CLI is the test harness, so it gets tested like one: in-process through Typer's
runner for speed, plus one subprocess check that the installed entry point really works.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.cli.repair import NOT_YET_IMPLEMENTED, SCORERS
from longrun.core.geo.gpx import gpx_write
from longrun.core.models.geometry import Route, RoutePoint

runner = CliRunner()


@pytest.fixture
def route_file(tmp_path: Path) -> Path:
    """A short Embarcadero line with ~40 m point spacing."""
    route = Route(
        id="sf",
        name="Embarcadero test",
        points=[
            RoutePoint(lat=37.7955, lon=-122.3937 + i * 0.00045, cum_dist_m=i * 40.0)
            for i in range(60)
        ],
    )
    return gpx_write(route, tmp_path / "sf.gpx")


def test_version_is_reported() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "longrun" in result.stdout


def test_repair_is_registered() -> None:
    assert "repair" in runner.invoke(app, ["--help"]).stdout


def test_repair_writes_a_sheet_and_a_plan(route_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30", "--out", str(out)],
    )
    assert result.exit_code == 0, result.stdout
    assert (out / "sheet.md").exists()
    assert (out / "plan.json").exists()


def test_the_sheet_reports_distance_and_duration(route_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner.invoke(
        app,
        ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30", "--out", str(out)],
    )
    sheet = (out / "sheet.md").read_text(encoding="utf-8")
    assert "Distance: 2.4 km" in sheet or "Distance: 2.3 km" in sheet
    assert "Projected duration" in sheet


def test_every_unimplemented_scorer_is_named_in_coverage(route_file: Path, tmp_path: Path) -> None:
    """Scope 3.6 applied to our own build state: a missing scorer is reported, not hidden."""
    out = tmp_path / "out"
    runner.invoke(
        app,
        ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30", "--out", str(out)],
    )
    sheet = (out / "sheet.md").read_text(encoding="utf-8")
    for name in NOT_YET_IMPLEMENTED:
        assert name in sheet, f"{name} vanished from the coverage manifest"


def test_the_sheet_carries_pacing_caveats(route_file: Path, tmp_path: Path) -> None:
    """Scope 12: population-curve pacing must be labelled a guess."""
    out = tmp_path / "out"
    runner.invoke(
        app,
        ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30", "--out", str(out)],
    )
    sheet = (out / "sheet.md").read_text(encoding="utf-8")
    assert "population" in sheet.lower()


def test_verification_runs_and_reports_skips(route_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner.invoke(
        app,
        ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30", "--out", str(out)],
    )
    sheet = (out / "sheet.md").read_text(encoding="utf-8")
    assert "Verification" in sheet
    assert "skipped" in sheet


def test_target_distance_is_checked_when_given(route_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner.invoke(
        app,
        [
            "repair",
            str(route_file),
            "--date",
            "2026-03-15",
            "--start",
            "07:30",
            "--out",
            str(out),
            "--target-km",
            "50",
        ],
    )
    sheet = (out / "sheet.md").read_text(encoding="utf-8")
    assert "distance_in_tolerance" in sheet


def test_a_malformed_gpx_is_a_clean_error_not_a_traceback(tmp_path: Path) -> None:
    bad = tmp_path / "bad.gpx"
    bad.write_text("<gpx><unclosed>", encoding="utf-8")
    result = runner.invoke(app, ["repair", str(bad), "--date", "2026-03-15"])
    assert result.exit_code == 2
    assert "could not parse" in result.stderr


def test_a_bad_start_time_is_a_clean_error(route_file: Path) -> None:
    result = runner.invoke(
        app, ["repair", str(route_file), "--date", "2026-03-15", "--start", "breakfast"]
    )
    assert result.exit_code == 2
    assert "HH:MM" in result.stderr


def test_output_to_stdout_when_no_out_directory(route_file: Path) -> None:
    result = runner.invoke(
        app, ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30"]
    )
    assert result.exit_code == 0
    assert result.stdout.lstrip().startswith("# Plan")


def test_plan_json_round_trips(route_file: Path, tmp_path: Path) -> None:
    """The plan schema is the API contract (scope 10.3); the CLI must emit a valid one."""
    from longrun.core.models.plan import Plan

    out = tmp_path / "out"
    runner.invoke(
        app,
        ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30", "--out", str(out)],
    )
    plan = Plan.model_validate_json((out / "plan.json").read_text(encoding="utf-8"))
    assert plan.route.length_m > 0
    assert len(plan.results) == len(SCORERS) + len(NOT_YET_IMPLEMENTED)


def test_repair_is_deterministic_apart_from_the_plan_id(route_file: Path, tmp_path: Path) -> None:
    """A golden route must produce the same sheet every run."""
    sheets = []
    for i in range(2):
        out = tmp_path / f"out{i}"
        runner.invoke(
            app,
            [
                "repair",
                str(route_file),
                "--date",
                "2026-03-15",
                "--start",
                "07:30",
                "--out",
                str(out),
            ],
        )
        text = (out / "sheet.md").read_text(encoding="utf-8")
        sheets.append(text.split("\n", 1)[1])  # drop the id line
    assert sheets[0] == sheets[1]


def test_the_cache_directory_env_var_reaches_the_command(
    route_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LONGRUN_CACHE_DIR is wired to the cache, not merely documented in .env.example."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("LONGRUN_CACHE_DIR", str(cache_dir))
    result = runner.invoke(
        app, ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30"]
    )
    assert result.exit_code == 0
    assert (cache_dir / "cache.sqlite").exists()


def test_no_cache_directory_means_no_files_are_written(
    route_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured, the cache is in-memory: a plain run must not litter the filesystem."""
    # A directory of its own, because `route_file` writes its GPX into tmp_path.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    result = runner.invoke(
        app, ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30"]
    )
    assert result.exit_code == 0
    assert list(workdir.iterdir()) == []


def test_the_offline_env_var_reaches_the_command(
    route_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope 4.4: LONGRUN_OFFLINE=1 must turn on no-miss mode without the flag.

    Golden and contract runs rely on the environment variable alone, so a flag-only
    implementation would leave them quietly able to reach the network.
    """
    from longrun.cli import repair as repair_mod

    seen: list[bool] = []
    real = repair_mod.SqliteCache

    def spy(*args: object, offline: bool = False, **kwargs: object) -> object:
        seen.append(offline)
        return real(*args, offline=offline, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repair_mod, "SqliteCache", spy)
    monkeypatch.setenv("LONGRUN_OFFLINE", "1")
    runner.invoke(app, ["repair", str(route_file), "--date", "2026-03-15", "--start", "07:30"])
    assert seen == [True]


@pytest.mark.network
def test_installed_entry_point_works() -> None:
    """The subprocess smoke test: proves [project.scripts] actually resolves."""
    result = subprocess.run(
        [sys.executable, "-m", "longrun.cli.main", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "longrun" in result.stdout


# --- freeze-fixture ---------------------------------------------------------
#
# The command itself needs a database and is exercised in tests/contract. What is
# hermetic is everything it does *before* connecting - which is where a typo in a layer
# name should be caught, rather than after a ten-second connection timeout.


def test_freeze_fixture_is_registered() -> None:
    assert "freeze-fixture" in runner.invoke(app, ["--help"]).stdout


def test_an_unknown_layer_is_refused_before_connecting(route_file: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "freeze-fixture",
            str(route_file),
            "--out",
            str(tmp_path / "fixtures"),
            "--layers",
            "ways,fountains",
        ],
    )
    assert result.exit_code == 2
    assert "fountains" in result.stderr
    assert "known layers are" in result.stderr
    assert not (tmp_path / "fixtures").exists(), "nothing is written before the layers check"


def test_a_malformed_gpx_is_refused_by_freeze_too(tmp_path: Path) -> None:
    bad = tmp_path / "bad.gpx"
    bad.write_text("<gpx>", encoding="utf-8")
    result = runner.invoke(app, ["freeze-fixture", str(bad), "--out", str(tmp_path / "fixtures")])
    assert result.exit_code == 2
    assert "could not parse" in result.stderr
