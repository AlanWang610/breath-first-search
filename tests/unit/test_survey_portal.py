"""`longrun survey-portal`: reads a portal, prints, and never writes (M21)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from longrun.adapters.portals.catalog import CATALOG
from longrun.cli.main import app

VIEW = {
    "name": "Temporary Street Closures",
    "rowsUpdatedAt": 1793059200,
    "licenseId": "PDDL",
    "license": {"name": "Open Data Commons Public Domain Dedication and License"},
    "columns": [
        {"fieldName": "start_dt", "dataTypeName": "calendar_date"},
        {"fieldName": "end_dt", "dataTypeName": "calendar_date"},
        {"fieldName": "shape", "dataTypeName": "line"},
    ],
}


@respx.mock
def test_a_socrata_dataset_is_described_with_its_licence_verbatim_and_a_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get("https://data.example.gov/api/views/abcd-1234.json").mock(
        return_value=httpx.Response(200, json=VIEW)
    )
    respx.get("https://data.example.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(200, json=[{"count": "3992"}])
    )
    monkeypatch.chdir(tmp_path)
    before = CATALOG.read_bytes()

    result = CliRunner().invoke(
        app, ["survey-portal", "socrata", "data.example.gov", "--dataset", "abcd-1234"]
    )

    assert result.exit_code == 0, result.output
    assert "licenseId (verbatim): 'PDDL'" in result.output
    assert "a payload may be committed" in result.output
    assert "rows: 3992; rows last updated 2026-10-27" in result.output
    stub = result.output.split("stub (nothing written", 1)[1].split("\n", 1)[1]
    (row,) = yaml.safe_load("\n".join(line[4:] for line in stub.splitlines()))
    assert row["geometry"] == {"kind": "field", "field": "shape"}
    assert row["start"]["field"] == "start_dt" and row["end"]["field"] == "end_dt"
    assert row["licence"]["id"] == "PDDL"
    assert os.listdir(tmp_path) == [], "a survey writes nothing"
    assert CATALOG.read_bytes() == before, "and least of all the catalog"


@respx.mock
def test_an_arcgis_service_lists_its_layers_and_a_layer_is_never_committable() -> None:
    service = "https://services.example.com/x/arcgis/rest/services/Closures/FeatureServer"
    respx.get(service).mock(
        return_value=httpx.Response(
            200,
            json={
                "layers": [{"id": 1, "name": "Closures", "geometryType": "esriGeometryPolyline"}]
            },
        )
    )
    respx.get(f"{service}/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Closures",
                "geometryType": "esriGeometryPolyline",
                "fields": [{"name": "starttime", "type": "esriFieldTypeDate"}],
                "editingInfo": {"lastEditDate": 1793059200000},
            },
        )
    )
    respx.get(f"{service}/1/query").mock(return_value=httpx.Response(200, json={"count": 12}))
    runner = CliRunner()

    listed = runner.invoke(app, ["survey-portal", "arcgis", service])
    assert f"{service}/1  Closures (esriGeometryPolyline)" in listed.output

    described = runner.invoke(app, ["survey-portal", "arcgis", f"{service}/1"])
    assert described.exit_code == 0, described.output
    assert "rows: 12; last edited 2026-10-27" in described.output
    assert "not committable (ADR 0061)" in described.output


def test_an_arcgis_search_needs_a_box() -> None:
    result = CliRunner().invoke(app, ["survey-portal", "arcgis", "-", "--query", "closure"])
    assert result.exit_code == 2
