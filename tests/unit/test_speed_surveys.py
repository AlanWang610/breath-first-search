"""Speed surveys matched to the ways they measured (M18, ADR 0053).

Hand-built lines near Kansas City with answers known by construction. The matcher is the
one part of the merge that fails silently - a survey on the wrong street raises a level
nobody can see was raised for the wrong reason - so its three rules get a case each, and
the build step's wiring is held with a fake adapter and no database.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from shapely.geometry import LineString

from longrun.core.models.features import Feature
from longrun.regions.speed_surveys import (
    Candidate,
    SurveyMatch,
    match_survey,
    normalise_name,
    resolve_conflicts,
    usable,
    vintage_of,
)

LON, SOUTH, NORTH = -94.58, 39.100, 39.103
#: Metres east, in degrees of longitude at this latitude.
M = 1 / 86_400


def _survey(**kwargs: Any) -> Feature:
    base: dict[str, Any] = {
        "kind": "speed_survey",
        "category": "speed_survey",
        "geometry": {"type": "LineString", "coordinates": [[LON, SOUTH], [LON, NORTH]]},
        "tier": 3,
        "confidence": 0.9,
        "measures": {"speed_85_kph": 61.0},
        "name": "Main St",
        "observed_on": date(2024, 5, 7),
    }
    return Feature(**{**base, **kwargs})


def _way(way_id: int, coords: list[tuple[float, float]], **tags: Any) -> Candidate:
    return Candidate(way_id=way_id, tags={"highway": "secondary", **tags}, line=LineString(coords))


def _ids(matches: list[SurveyMatch]) -> list[int]:
    return [m.way_id for m in matches]


def test_the_street_the_survey_measured_is_matched() -> None:
    [match] = match_survey(
        _survey(), [_way(1, [(LON, SOUTH), (LON, NORTH)], name="Main Street")], "s"
    )
    assert match.speed_85_kph == 61.0
    assert match.offset_m < 1.0 and match.bearing_diff_deg < 1.0
    assert match.name_matched


def test_fifteen_metres_is_the_reach() -> None:
    near = _way(2, [(LON + 14 * M, SOUTH), (LON + 14 * M, NORTH)])
    far = _way(3, [(LON + 16 * M, SOUTH), (LON + 16 * M, NORTH)])
    assert _ids(match_survey(_survey(name=None), [near, far], "s")) == [2]


def test_a_cross_street_is_not_the_street_measured() -> None:
    """It meets the survey's reach - it crosses it - and runs the wrong way."""
    cross = _way(4, [(LON - 200 * M, 39.1015), (LON + 200 * M, 39.1015)])
    assert match_survey(_survey(name=None), [cross], "s") == []


def test_a_way_that_continues_the_line_past_the_survey_is_a_different_stretch() -> None:
    """Collinear and adjacent, but only its first few metres lie inside the reach."""
    beyond = _way(5, [(LON, NORTH - 0.0001), (LON, NORTH + 0.007)])
    assert match_survey(_survey(name=None), [beyond], "s") == []


def test_a_line_digitised_the_other_way_round_is_the_same_street() -> None:
    backwards = _way(6, [(LON, NORTH), (LON, SOUTH)])
    assert _ids(match_survey(_survey(name=None), [backwards], "s")) == [6]


def test_two_named_streets_side_by_side_are_told_apart_by_name() -> None:
    """A frontage road 12 m from the arterial is inside the reach and parallel; the name is
    the only thing that says which one was counted."""
    arterial = _way(7, [(LON, SOUTH), (LON, NORTH)], name="Main Street")
    frontage = _way(8, [(LON + 12 * M, SOUTH), (LON + 12 * M, NORTH)], name="Oak St")
    assert _ids(match_survey(_survey(), [arterial, frontage], "s")) == [7]


def test_a_way_with_no_name_is_matched_on_geometry_and_says_so() -> None:
    [match] = match_survey(_survey(), [_way(9, [(LON, SOUTH), (LON, NORTH)])], "s")
    assert not match.name_matched


@pytest.mark.parametrize(
    ("one", "other"),
    [("Main St", "MAIN STREET"), ("N. Oak Ave.", "north oak avenue"), ("US 71", "US-71")],
)
def test_names_compare_the_way_people_write_them(one: str, other: str) -> None:
    assert normalise_name(one) == normalise_name(other)


def test_only_a_trustworthy_survey_may_steer_a_graph() -> None:
    """A tier-4 speed read off a PDF by a model is a lead, not a measurement."""
    assert usable(_survey())
    assert not usable(_survey(tier=4, confidence=0.5))
    assert not usable(_survey(confidence=0.7))


def _match(**kwargs: Any) -> SurveyMatch:
    base: dict[str, Any] = {
        "way_id": 1,
        "speed_85_kph": 50.0,
        "source": "a",
        "survey_ref": None,
        "observed_on": date(2023, 1, 1),
        "tier": 3,
        "confidence": 0.9,
        "offset_m": 0.0,
        "bearing_diff_deg": 0.0,
        "name_matched": True,
    }
    return SurveyMatch(**{**base, **kwargs})


def test_one_survey_per_way_the_better_source_first() -> None:
    tier_two = _match(tier=2, source="state", speed_85_kph=40.0)
    surer = _match(confidence=0.95, source="surer")
    newer = _match(observed_on=date(2025, 6, 1), source="newer")
    faster = _match(speed_85_kph=70.0, source="faster")
    assert [m.source for m in resolve_conflicts([faster, newer, surer, tier_two])] == ["state"]
    assert [m.source for m in resolve_conflicts([faster, newer, surer])] == ["surer"]
    assert [m.source for m in resolve_conflicts([faster, newer])] == ["newer"]
    assert [m.source for m in resolve_conflicts([_match(), faster])] == ["faster"]


def test_the_vintage_is_the_content_so_an_identical_rebuild_keeps_it() -> None:
    rows = [_match(way_id=2), _match(way_id=1)]
    assert vintage_of(rows) == vintage_of(list(rows))
    assert vintage_of(rows) != vintage_of([_match(way_id=2, speed_85_kph=51.0), _match()])
    assert vintage_of([]) is None


# --- the build step, with a fake adapter and no database ---------------------


def test_the_merge_runs_after_the_layers_it_reads_and_before_the_scoring_that_reads_it() -> None:
    from longrun.regions.build import STEPS

    assert STEPS.index("layers") < STEPS.index("speed_surveys") < STEPS.index("way_lts")


class _Surveys:
    """A speed-survey adapter for Kansas City that hands back one survey."""

    name = "portal.fake.kcmo-speeds"
    kind = "speed_survey"
    tier = 3
    jurisdictions = ("tiger:place:2938000",)
    source = "fake"
    scope = "feed"

    def fetch(self, polygon: Any, day: date, ctx: Any) -> Any:
        from longrun.adapters.base import AdapterResult

        return AdapterResult(features=[_survey(jurisdiction="tiger:place:2938000")])


def _build_context(tmp_path: Any, adapters: list[Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    import longrun.adapters.registry as registry_mod
    from longrun.core.data.jurisdictions import from_tiger_row
    from longrun.regions import build
    from longrun.regions.build import BuildContext, BuildManifest, RegionSpec, StepRecord

    places = [
        from_tiger_row("state", "29", "Missouri", "29"),
        from_tiger_row("place", "2938000", "Kansas City", "29"),
    ]
    monkeypatch.setattr(build, "jurisdictions_in", lambda ctx: places)

    def with_fakes(cache: Any, budget: Any, **kwargs: Any) -> Any:
        assert kwargs["max_jurisdictions"] is None and kwargs["max_fetches"] is None
        return registry_mod.AdapterRegistry(cache, budget, offline=False, adapters=adapters)

    monkeypatch.setattr(registry_mod, "build_registry", with_fakes)
    polygon = tmp_path / "region.geojson"
    polygon.write_text(
        '{"type": "Polygon", "coordinates": [[[-94.7, 39.0], [-94.4, 39.0], [-94.4, 39.2], '
        "[-94.7, 39.2], [-94.7, 39.0]]]}",
        encoding="utf-8",
    )
    manifest = BuildManifest(region="kc")
    manifest.record("way_lts", StepRecord(status="done"))
    return BuildContext(
        spec=RegionSpec(name="kc", polygon=polygon),
        connection=None,
        manifest=manifest,
        manifest_path=tmp_path / "manifest.json",
        log=lambda _: None,
    )


def test_a_region_no_survey_adapter_claims_asks_nobody(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longrun.regions.build import step_speed_surveys

    record = step_speed_surveys(_build_context(tmp_path, [], monkeypatch))
    assert record.status == "blocked"
    assert record.counts == {"asked": 2}


def test_a_claimed_survey_is_fetched_merged_and_sends_the_scoring_round_again(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longrun.regions import speed_surveys
    from longrun.regions.build import step_speed_surveys

    seen: list[Any] = []

    def compute(connection: Any, *, region: str, surveys: Any, log: Any) -> Any:
        seen.extend(surveys)
        return speed_surveys.SurveyReport(surveys=1, usable=1, merged=3, changed=True)

    monkeypatch.setattr(speed_surveys, "compute", compute)
    ctx = _build_context(tmp_path, [_Surveys()], monkeypatch)
    record = step_speed_surveys(ctx)
    assert record.status == "done"
    assert [f.measures for f in seen] == [{"speed_85_kph": 61.0}]
    assert record.counts["claimed"] == 1 and record.counts["merged"] == 3
    assert "way_lts" not in ctx.manifest.steps, "a changed merge has to be rescored"


def test_a_build_lifts_the_plans_ceilings() -> None:
    """Bay Area's 187 jurisdictions are a region's ordinary size and a plan's emergency."""
    from longrun.adapters.registry import MAX_JURISDICTIONS, AdapterRegistry
    from longrun.core.data.cache import SqliteCache
    from longrun.core.data.jurisdictions import from_tiger_row
    from longrun.core.models.context import Budget

    many = [from_tiger_row("place", f"29{i:05d}", f"Place {i}", "29") for i in range(60)]
    with SqliteCache() as cache:
        plan = AdapterRegistry(cache, Budget(), adapters=[]).fetch(
            "closures", many, None, date.today()
        )
        build = AdapterRegistry(cache, Budget(), adapters=[], max_jurisdictions=None).fetch(
            "closures", many, None, date.today()
        )
    capped = [a for a in plan.answers if "more than" in (a.reason or "")]
    assert len(capped) == 60 - MAX_JURISDICTIONS
    assert not [a for a in build.answers if "more than" in (a.reason or "")]
