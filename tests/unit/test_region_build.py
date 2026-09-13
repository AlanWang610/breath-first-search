"""The region build's spec, manifest and step orchestration (scope 13).

The steps themselves need PostGIS and a 233 MB extract, so they live in
`tests/contract/test_region_build.py`. What is here is everything that decides *whether a
step runs at all*, which is the part a resumed build depends on and the part that can be
tested with a dictionary.

Two behaviours carry the weight.

**`blocked` counts as complete.** A step whose input does not exist will not have it on
the next run either, and re-discovering that costs the whole build's progress. The
distinction between `blocked` and `failed` is the one a resumed build turns on, so it is
tested directly rather than inferred from a green build.

**A failure stops the build.** Steps 4 and 5 read what step 2 loaded, and a coverage report
written over a half-loaded database would be the one artefact of the build that lied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from longrun.regions.build import (
    STEPS,
    BuildContext,
    BuildManifest,
    RegionSpec,
    StepRecord,
    build_region,
)

POLYGON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-122.5, 37.7], [-122.3, 37.7], [-122.3, 37.9], [-122.5, 37.9], [-122.5, 37.7]]
                ],
            },
        }
    ],
}


def _spec_dir(tmp_path: Path, **extra: Any) -> Path:
    import yaml

    (tmp_path / "region.geojson").write_text(json.dumps(POLYGON), encoding="utf-8")
    body: dict[str, Any] = {"name": "testregion", "polygon": "region.geojson", **extra}
    path = tmp_path / "region.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


# --- the spec ---------------------------------------------------------------


def test_spec_paths_are_relative_to_the_spec(tmp_path: Path) -> None:
    """So a region moves with its data, and a build runs from any working directory."""
    (tmp_path / "extract.osm.pbf").write_bytes(b"not really a pbf")
    spec = RegionSpec.load(_spec_dir(tmp_path, osm_extract="extract.osm.pbf"))
    assert spec.osm_extract is not None
    assert spec.osm_extract.is_absolute()
    assert spec.osm_extract.exists()


def test_the_polygon_is_read_in_wgs84(tmp_path: Path) -> None:
    spec = RegionSpec.load(_spec_dir(tmp_path))
    west, south, east, north = spec.shape().bounds
    assert (round(west, 1), round(south, 1)) == (-122.5, 37.7)
    assert (round(east, 1), round(north, 1)) == (-122.3, 37.9)


def test_gtfs_feeds_are_a_mapping_of_id_to_path(tmp_path: Path) -> None:
    """The id is half the stop's primary key, so it has to be the author's choice and
    stable across builds - two agencies both numbering a stop `1` is the ordinary case."""
    (tmp_path / "bart.zip").write_bytes(b"")
    spec = RegionSpec.load(_spec_dir(tmp_path, gtfs={"bart": "bart.zip"}))
    assert set(spec.gtfs) == {"bart"}
    assert spec.gtfs["bart"].name == "bart.zip"


def test_a_spec_with_nothing_but_a_polygon_is_valid(tmp_path: Path) -> None:
    """Most fields are optional because most regions are built in stages."""
    spec = RegionSpec.load(_spec_dir(tmp_path))
    assert spec.osm_extract is None
    assert spec.huc4 == []


# --- the manifest -----------------------------------------------------------


def test_a_done_step_and_a_blocked_step_are_both_complete() -> None:
    """The distinction a resumed build turns on. A step whose input does not exist will
    not have it next time either, and re-discovering that costs the build's progress."""
    assert StepRecord(status="done").complete
    assert StepRecord(status="blocked").complete
    assert not StepRecord(status="failed").complete
    assert not StepRecord(status="skipped").complete


def test_a_manifest_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "build.json"
    manifest = BuildManifest(region="r")
    manifest.record("layers", StepRecord(status="done", counts={"ways": 3}))
    manifest.save(path)
    restored = BuildManifest.load_or_new(path, "r", path)
    assert restored.steps["layers"].counts == {"ways": 3}
    assert restored.steps["layers"].at


def test_a_corrupt_manifest_starts_a_new_build_rather_than_crashing(tmp_path: Path) -> None:
    """An unreadable record of what happened is not a reason to refuse to do it again."""
    path = tmp_path / "build.json"
    path.write_text("{ this is not json", encoding="utf-8")
    manifest = BuildManifest.load_or_new(path, "r", path)
    assert manifest.steps == {}


def test_a_manifest_is_complete_only_when_every_step_is() -> None:
    manifest = BuildManifest(region="r")
    for step in STEPS[:-1]:
        manifest.record(step, StepRecord(status="done"))
    assert not manifest.complete
    manifest.record(STEPS[-1], StepRecord(status="done"))
    assert manifest.complete


# --- orchestration ----------------------------------------------------------


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace the five steps with recorders, so orchestration is testable alone."""
    from longrun.regions import build as mod

    ran: dict[str, list[str]] = {"order": []}

    def recorder(name: str) -> Any:
        def run(ctx: BuildContext) -> StepRecord:
            ran["order"].append(name)
            return StepRecord(status="done", detail=name)

        return run

    monkeypatch.setattr(mod, "STEP_FUNCTIONS", {name: recorder(name) for name in STEPS})
    return ran


def _run(tmp_path: Path, **kwargs: Any) -> BuildManifest:
    spec = RegionSpec.load(_spec_dir(tmp_path))
    return build_region(spec, None, tmp_path / "build.json", log=lambda _: None, **kwargs)


def test_every_step_runs_in_the_order_the_scope_lists(tmp_path: Path, steps: dict) -> None:
    """Step 4 reads what step 2 loaded, so the order is a dependency and not a listing."""
    _run(tmp_path)
    assert steps["order"] == list(STEPS)


def test_a_second_run_skips_what_the_first_finished(tmp_path: Path, steps: dict) -> None:
    _run(tmp_path)
    steps["order"].clear()
    _run(tmp_path)
    assert steps["order"] == []


def test_force_re_runs_everything(tmp_path: Path, steps: dict) -> None:
    _run(tmp_path)
    steps["order"].clear()
    _run(tmp_path, force=True)
    assert steps["order"] == list(STEPS)


def test_only_runs_the_named_steps(tmp_path: Path, steps: dict) -> None:
    _run(tmp_path, only=["terrain"])
    assert steps["order"] == ["terrain"]


def test_a_failing_step_stops_the_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Steps 4 and 5 read what step 2 loaded. A coverage report written over a half-loaded
    database would be the one artefact of the build that lied."""
    from longrun.regions import build as mod

    ran: list[str] = []

    def ok(name: str) -> Any:
        def run(ctx: BuildContext) -> StepRecord:
            ran.append(name)
            return StepRecord(status="done")

        return run

    def boom(ctx: BuildContext) -> StepRecord:
        ran.append("layers")
        raise RuntimeError("the database went away")

    functions = {name: ok(name) for name in STEPS}
    functions["layers"] = boom
    monkeypatch.setattr(mod, "STEP_FUNCTIONS", functions)

    manifest = _run(tmp_path)
    assert ran == ["osm_graph", "layers"]
    assert manifest.steps["layers"].status == "failed"
    assert "the database went away" in manifest.steps["layers"].detail
    assert "terrain" not in manifest.steps


def test_a_failed_step_is_retried_on_the_next_run(tmp_path: Path, steps: dict) -> None:
    """The complement of the blocked rule: a failure is a thing that might work next time."""
    path = tmp_path / "build.json"
    manifest = BuildManifest(region="testregion")
    manifest.record("layers", StepRecord(status="failed", detail="transient"))
    manifest.save(path)
    _run(tmp_path)
    assert "layers" in steps["order"]


def test_the_manifest_is_written_after_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that only saved at the end would have nothing to resume from, which is the
    case resuming exists for."""
    from longrun.regions import build as mod

    seen: list[int] = []

    def counting(ctx: BuildContext) -> StepRecord:
        path = tmp_path / "build.json"
        # Absent before the first step, which is the point: the file appears as soon as
        # there is something to resume from, not at the end.
        recorded = json.loads(path.read_text())["steps"] if path.exists() else {}
        seen.append(len(recorded))
        return StepRecord(status="done")

    monkeypatch.setattr(mod, "STEP_FUNCTIONS", dict.fromkeys(STEPS, counting))
    _run(tmp_path)
    # Each step sees exactly its predecessors already on disk, so a build interrupted
    # after step n resumes at step n+1 rather than at the beginning.
    assert seen == list(range(len(STEPS)))
