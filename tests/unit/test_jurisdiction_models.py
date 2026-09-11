"""The vocabulary adapters and routes agree on (scope 7.10, 13 step 4).

No store, no registry, no network - this is the id grammar and the answer record, which is
where two whole subsystems agree about who owns a piece of ground. The parts that carry
risk are the two that look like string formatting and are not: a PAD-US code that names a
class rather than an agency, and a GEOID that is a lexical prefix of a different GEOID.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from longrun.core.models.features import FeatureSet, JurisdictionAnswer
from longrun.core.models.jurisdiction import (
    FEDERAL_AGENCY_CODES,
    AdapterInfo,
    Jurisdiction,
    is_unknown_agency,
    padus_id,
    tiger_id,
)

# --- the id grammar ---------------------------------------------------------


def test_a_federal_agency_code_is_national() -> None:
    """`NPS` is the National Park Service in Yosemite and in Acadia. Qualifying it by state
    would fragment one agency into fifty adapters each covering a fiftieth of it."""
    assert padus_id("NPS", "06") == "padus:NPS"
    assert padus_id("NPS") == "padus:NPS"
    assert padus_id("USFS", "29") == "padus:USFS"


def test_a_non_federal_code_carries_its_state() -> None:
    """The finding that changed the design. PAD-US `Mang_Name` is a coded domain, and the
    committed fixtures hold `CITY`, `JNT`, `NGO`, `PVT`, `SDOL`. `CITY` names a *class* of
    manager, so `padus:CITY` would resolve every municipal park in America to one adapter.
    """
    assert padus_id("CITY", "06") == "padus:CITY:06"
    assert padus_id("SDOL", "04") == "padus:SDOL:04"
    assert padus_id("CITY", "06") != padus_id("CITY", "29")


def test_a_stateless_local_code_degrades_rather_than_claiming_the_country() -> None:
    """When a park's state cannot be resolved the id loses its qualifier and the caller
    records why. Worse than knowing, better than inventing a state."""
    assert padus_id("CITY") == "padus:CITY"


def test_codes_are_normalised_so_one_agency_is_one_id() -> None:
    assert padus_id(" nps ") == "padus:NPS"
    assert padus_id("city", "06") == "padus:CITY:06"


def test_unknown_agencies_are_recognised_as_naming_nobody() -> None:
    """PAD-US spells "unknown" several ways, and a park managed by nobody-in-particular
    must not become a jurisdiction an adapter can claim."""
    for code in ("UNK", "UNKL", "", "unknown", None):
        assert is_unknown_agency(code), code
    assert not is_unknown_agency("NPS")
    assert not is_unknown_agency("CITY")


def test_every_federal_code_is_uppercase() -> None:
    """`padus_id` upper-cases before testing membership, so a lowercase entry in the set
    would be unreachable and that agency would silently gain a state qualifier."""
    assert all(code == code.upper() for code in FEDERAL_AGENCY_CODES)


# --- containment, and the prefix trap ---------------------------------------


def test_a_county_adapter_does_not_match_a_place_whose_geoid_extends_it() -> None:
    """The trap this grammar exists to avoid. County `29095` is a lexical prefix of the
    possible place `2909512`, so matching adapters by `startswith` on raw GEOIDs would
    silently claim a county adapter covers a city it may have nothing to do with.
    Containment is declared in `within`, where it can be checked."""
    county = tiger_id("county", "29095")
    lookalike = Jurisdiction(
        id=tiger_id("place", "2909512"), level="place", name="Lookalike", within=()
    )
    assert lookalike.id.startswith("tiger:place:29095"), "the lexical trap is real"
    assert county not in lookalike.ids, "but membership is exact, so it does not fire"


def test_ids_expose_the_jurisdiction_and_everything_containing_it() -> None:
    """What lets one statewide WZDx fetch answer for every county and place inside the
    state, rather than one fetch per place."""
    kc = Jurisdiction(
        id="tiger:place:2938000",
        level="place",
        name="Kansas City",
        within=("tiger:county:29095", "tiger:state:29"),
    )
    assert kc.ids == ("tiger:place:2938000", "tiger:county:29095", "tiger:state:29")
    assert "tiger:state:29" in kc.ids
    assert "tiger:state:20" not in kc.ids, "a Missouri city is not covered by Kansas"


def test_a_jurisdiction_is_frozen() -> None:
    j = Jurisdiction(id=tiger_id("state", "29"), level="state", name="Missouri")
    with pytest.raises(ValidationError):
        j.id = "tiger:state:20"  # type: ignore[misc]


# --- the answer record ------------------------------------------------------


def test_an_answer_renders_name_and_id_because_neither_alone_identifies() -> None:
    """There is a Kansas City in Missouri and a Kansas City in Kansas, they share a border,
    and scope 11's state-line region is built out of that pair. A sheet naming only the
    name is ambiguous; one naming only the id is unreadable."""
    mo = JurisdictionAnswer(
        jurisdiction="tiger:place:2938000", name="Kansas City", kind="closures", checked=True
    )
    ks = JurisdictionAnswer(
        jurisdiction="tiger:place:2036000", name="Kansas City", kind="closures", checked=True
    )
    assert mo.label() != ks.label()
    assert "Kansas City" in mo.label() and "2938000" in mo.label()


def test_an_answer_without_a_name_still_identifies() -> None:
    assert JurisdictionAnswer(jurisdiction="padus:NPS", kind="trail_status").label() == "padus:NPS"


def test_an_answer_becomes_the_coverage_entry_m1_left_unfillable() -> None:
    """`CoverageEntry.jurisdiction` and `.tier` have existed since M1 and been passed by
    nothing. This is what fills them."""
    entry = JurisdictionAnswer(
        jurisdiction="tiger:state:29",
        name="Missouri",
        kind="closures",
        checked=True,
        tier=1,
        adapter="wzdx.modot",
        vintage="wzdx-4.1",
    ).coverage("closures")
    assert entry.checked and entry.tier == 1
    assert entry.jurisdiction is not None and "Missouri" in entry.jurisdiction
    assert "NOT CHECKED" not in str(entry)


def test_an_unanswered_jurisdiction_reads_as_not_checked() -> None:
    entry = JurisdictionAnswer(
        jurisdiction="tiger:county:20209",
        name="Wyandotte County",
        kind="closures",
        reason="no adapter",
    ).coverage("closures")
    assert not entry.checked
    assert entry.tier is None, "nobody answering is not the same as tier 4 answering"
    assert "NOT CHECKED" in str(entry) and "no adapter" in str(entry)


# --- FeatureSet consistency -------------------------------------------------


def test_from_answers_keeps_the_summary_fields_from_drifting() -> None:
    """`queried` and `missing_adapters` stay real fields because callers read them, but
    nothing fills them by hand - which is how they would come to disagree with `answers`."""
    answers = [
        JurisdictionAnswer(jurisdiction="tiger:state:29", kind="closures", checked=True, tier=1),
        JurisdictionAnswer(jurisdiction="tiger:state:20", kind="closures", checked=True, tier=1),
        JurisdictionAnswer(jurisdiction="padus:CITY:29", kind="closures", reason="no adapter"),
    ]
    found = FeatureSet.from_answers([], answers)
    assert found.queried == ["tiger:state:29", "tiger:state:20"]
    assert found.missing_adapters == ["padus:CITY:29"]
    assert found.answered


def test_nobody_asked_is_not_nothing_found() -> None:
    """Scope 7.10's distinction, now at per-jurisdiction resolution."""
    assert not FeatureSet().answered
    asked = FeatureSet.from_answers(
        [], [JurisdictionAnswer(jurisdiction="tiger:state:29", kind="closures", checked=True)]
    )
    assert asked.answered and not asked.features


def test_adapter_info_describes_an_adapter_without_importing_one() -> None:
    """Scope 13 ends "the only California-specific code in the repository lives in adapters
    registered for California jurisdictions", so a region build reports adapter coverage
    through this record rather than by importing the adapter package."""
    assert AdapterInfo(name="wzdx.modot", tier=1, kind="closures", source="wzdx_modot").tier == 1
