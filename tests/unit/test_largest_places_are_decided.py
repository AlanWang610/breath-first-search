"""Every one of the fifty largest places is decided, for closures and for speed surveys (M21).

The scope's tier 3 targets "the top 50 metros". M17.6 chose the fifty most populous
incorporated places from the committed national table, since there is no CBSA source; this
holds each of them to an adopted catalog row or a refusal with a reason from a closed set
and what was checked - so "nobody looked" can never pass for "nothing there", and the gap
between the two is a number a later milestone can shrink.
"""

from __future__ import annotations

import pytest

from longrun.adapters.portals.catalog import CATALOG, read
from longrun.regions.coverage import coverage_table, largest, load_table

KINDS = ("closures", "speed_survey")


@pytest.fixture(scope="module")
def places() -> list[tuple[str, str, tuple[str, ...]]]:
    """(id, name, ids it sits in) for the fifty most populous places."""
    table = load_table()
    by_id = {e.id: e for e in table}
    rows = largest(coverage_table([], table, ["closures"]), "closures", 50)
    out = []
    for row in rows:
        jurisdiction = by_id[row.id].jurisdiction
        assert jurisdiction is not None
        out.append((row.id, row.name, tuple(jurisdiction.ids)))
    return out


def test_the_catalog_decides_every_one_of_the_fifty_largest_places(
    places: list[tuple[str, str, tuple[str, ...]]],
) -> None:
    catalog = read(CATALOG)
    assert catalog.failures == [], catalog.failures
    adopted = {(e.jurisdiction, e.kind) for e in catalog.adopted}
    refused = {(r.jurisdiction, r.kind) for r in catalog.refused}

    undecided = [
        f"{name} ({place}) {kind}"
        for place, name, ids in places
        for kind in KINDS
        if not any((i, kind) in adopted for i in ids) and (place, kind) not in refused
    ]
    assert undecided == [], "adopt a dataset or record a refusal: " + "; ".join(undecided)


def test_no_place_is_both_adopted_and_refused_for_one_kind() -> None:
    catalog = read(CATALOG)
    adopted = {(e.jurisdiction, e.kind) for e in catalog.adopted}
    both = sorted(adopted & {(r.jurisdiction, r.kind) for r in catalog.refused})
    assert both == []


def test_a_place_is_refused_once_per_kind() -> None:
    keys = [(r.jurisdiction, r.kind) for r in read(CATALOG).refused]
    assert len(keys) == len(set(keys))


def test_a_covered_refusal_names_a_better_tier_that_exists() -> None:
    """`covered` is a claim about another adapter, so it is checked against the registry
    rather than trusted: something at tier 1 or 2 must claim the place for the kind."""
    from longrun.adapters.registry import discover

    adapters, _ = discover()
    for refusal in read(CATALOG).refused:
        if refusal.reason != "covered":
            continue
        better = [
            a.name
            for a in adapters
            if a.kind == refusal.kind
            and int(a.tier) < 3
            and refusal.jurisdiction in a.jurisdictions
        ]
        assert better, f"{refusal.jurisdiction} {refusal.kind} is 'covered' by nothing"


def test_the_decisions_are_counted(places: list[tuple[str, str, tuple[str, ...]]]) -> None:
    """Pinned so a change in either direction is a decision someone saw: M21 adopted 11 of
    the 50 for closures and 1 for speed surveys. Raise these in the commit that earns it."""
    catalog = read(CATALOG)
    largest_ids = {p for p, _, _ in places}
    for kind, at_least in (("closures", 11), ("speed_survey", 1)):
        adopted = {e.jurisdiction for e in catalog.adopted if e.kind == kind} & largest_ids
        assert len(adopted) >= at_least, (kind, sorted(adopted))


def test_largest_prints_each_places_decision() -> None:
    from typer.testing import CliRunner

    from longrun.cli.main import app

    result = CliRunner().invoke(
        app, ["adapter-coverage", "--kind", "speed_survey", "--largest", "50"]
    )
    assert result.exit_code == 0, result.output
    assert "Oakland city" in result.output
    assert "catalog: adopted portal.arcgis.oakland-85th-percentile-speeds" in result.output
    assert "catalog: refused (not_a_survey)" in result.output
