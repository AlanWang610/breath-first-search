"""National adapter coverage, measured against every US jurisdiction (M17).

The table builder is tested on a hand-written miniature of the two Census files, so what
is pinned is each rule the builder makes - which summary levels become rows, where a
place's counties come from, which duplicates collapse. The measurement is tested with fake
adapters, because what matters is that it counts the way a plan's registry matches. The
committed table and the real registry get their own checks at the end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from longrun.adapters.base import AdapterResult
from longrun.regions.coverage import (
    TABLE,
    build_table,
    coverage_table,
    largest,
    load_table,
    summarise,
    write_table,
)

SUB_HEADER = (
    "SUMLEV,STATE,COUNTY,PLACE,COUSUB,CONCIT,PRIMGEO_FLAG,FUNCSTAT,NAME,STNAME,POPESTIMATE2023"
)
CO_HEADER = "SUMLEV,STATE,COUNTY,STNAME,CTYNAME,POPESTIMATE2023"


def _census(tmp_path: Path) -> tuple[Path, Path]:
    sub = tmp_path / "sub.csv"
    sub.write_text(
        "\n".join(
            [
                SUB_HEADER,
                "040,29,000,00000,00000,00000,0,A,Missouri,Missouri,6000000",
                "162,29,000,38000,00000,00000,0,A,Kansas City city,Missouri,510000",
                "157,29,095,38000,00000,00000,0,A,Kansas City city,Missouri,400000",
                "157,29,047,38000,00000,00000,0,A,Kansas City city,Missouri,110000",
                # A consolidated city's part, listed again under 172: one row survives.
                "172,29,000,38000,00000,99999,0,A,Kansas City city,Missouri,510000",
                "061,29,095,00000,12345,00000,0,A,Blue township,Missouri,9000",
                "061,29,095,00000,54321,00000,0,N,Defunct township,Missouri,10",
            ]
        ),
        encoding="latin-1",
    )
    co = tmp_path / "co.csv"
    co.write_text(
        "\n".join(
            [
                CO_HEADER,
                "040,29,000,Missouri,Missouri,6000000",
                "050,29,095,Missouri,Jackson County,700000",
                "050,29,047,Missouri,Clay County,250000",
            ]
        ),
        encoding="latin-1",
    )
    return sub, co


def test_the_table_holds_governments_and_places_learn_their_counties(tmp_path: Path) -> None:
    rows = build_table(*_census(tmp_path))
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {
        "tiger:state:29",
        "tiger:county:29095",
        "tiger:county:29047",
        "tiger:place:2938000",
        "cousub:2909512345",
    }, "a non-functioning township is not a government, and the 172 repeat collapses"
    assert by_id["tiger:place:2938000"]["counties"] == "29047 29095"
    assert by_id["tiger:county:29095"]["population"] == "700000"


def test_the_table_round_trips_into_the_jurisdictions_a_plan_would_mint(tmp_path: Path) -> None:
    path = tmp_path / "t.csv.gz"
    write_table(build_table(*_census(tmp_path)), path)
    loaded = {e.id: e for e in load_table(path)}
    kc = loaded["tiger:place:2938000"].jurisdiction
    assert kc is not None
    assert kc.within == ("tiger:state:29", "tiger:county:29047", "tiger:county:29095")
    assert loaded["cousub:2909512345"].jurisdiction is None, "no id level exists for towns"


def test_rebuilding_the_same_data_is_the_same_bytes(tmp_path: Path) -> None:
    rows = build_table(*_census(tmp_path))
    write_table(rows, tmp_path / "a.gz")
    write_table(rows, tmp_path / "b.gz")
    assert (tmp_path / "a.gz").read_bytes() == (tmp_path / "b.gz").read_bytes()


class _Fake:
    def __init__(self, name: str, claims: tuple[str, ...], tier: int = 1, key: Any = None):
        self.name, self.jurisdictions, self.tier, self.key = name, claims, tier, key
        self.kind, self.source = "closures", "fake"

    def fetch(self, polygon: Any, day: Any, ctx: Any) -> AdapterResult:
        return AdapterResult()


class _Key:
    def value(self) -> None:
        return None


def test_a_county_adapter_covers_its_cities_and_a_missing_key_covers_nothing(
    tmp_path: Path,
) -> None:
    """Matched with the registry's own `matches`, so the report counts what a plan finds."""
    path = tmp_path / "t.csv.gz"
    write_table(build_table(*_census(tmp_path)), path)
    table = load_table(path)
    jackson = _Fake("wzdx.jackson", ("tiger:county:29095",))
    clay = _Fake("wzdx.clay", ("tiger:county:29047",), key=_Key())
    rows = coverage_table([jackson, clay], table, ["closures"])
    by_id = {r.id: r for r in rows}
    assert by_id["tiger:place:2938000"].tier == 1
    assert by_id["tiger:county:29047"].key_missing
    assert by_id["tiger:state:29"].tier is None

    summary = summarise(rows, table)["closures"]
    assert summary["county_population_by_tier"] == {"1": 700000}, "Clay's key is not set"
    assert summary["county_population_share"] == pytest.approx(700000 / 950000, abs=1e-4)
    assert summary["states_uncovered"] == ["29"]
    assert [r.id for r in largest(rows, "closures", 1)] == ["tiger:place:2938000"]


# --- the committed table and the real registry ------------------------------


def test_the_committed_table_is_whole_and_consistent() -> None:
    """Every id unique, fifty states and DC, and every county a place names exists."""
    table = load_table(TABLE)
    ids = [e.id for e in table]
    assert len(ids) == len(set(ids))
    assert sum(1 for e in table if e.level == "state") == 51
    counties = {e.id for e in table if e.level == "county"}
    assert len(counties) > 3000
    for entry in table:
        if entry.level == "place" and entry.jurisdiction is not None:
            assert all(
                i in counties for i in entry.jurisdiction.within if i.startswith("tiger:county")
            ), entry.id


def test_statewide_closure_coverage_never_falls() -> None:
    """The ratchet. M17 measured 20 states answered at tier 1 with no key needed; every
    later milestone may raise this number and none may lower it. Update it upward in the
    commit that earns it."""
    from longrun.adapters.registry import discover

    adapters, _ = discover()
    table = load_table(TABLE)
    summary = summarise(coverage_table(adapters, table, ["closures"]), table)["closures"]
    usable = summary["states_by_tier"]["1"] + summary["states_by_tier"]["2"]
    assert len(usable) >= 20, sorted(usable)


def test_the_command_reports_as_json() -> None:
    from typer.testing import CliRunner

    from longrun.cli.main import app

    result = CliRunner().invoke(app, ["adapter-coverage", "--kind", "closures", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["closures"]["places_total"] > 19000
    assert {"tier", "adapters", "key_missing"} <= set(payload["rows"][0])
