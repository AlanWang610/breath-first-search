"""Which portal payloads may be committed, and the evidence each needs (M21, ADR 0061).

ADR 0038 accepts a grant shipped inside the payload. A Socrata row has no envelope to ship
one in, so for a portal the grant is the dataset's own metadata - `licenseId` in
`/api/views/<id>.json` - committed **verbatim** beside the payload as `<name>.meta.json`, so
a reviewer reads the publisher's words and not this project's summary of them. Only a
public-domain dedication qualifies (`catalog.COMMITTABLE_LICENCES`); ArcGIS publishes its
licence as free HTML and never qualifies.

The same rule reaches the goldens. A golden's `cache.sqlite` is a committed payload too,
and a portal answer recorded into one must come from a dataset whose catalog row declares a
committable licence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from longrun.adapters.portals.catalog import (
    CATALOG,
    COMMITTABLE_LICENCES,
    committable,
    read,
)

CASSETTES = Path(__file__).parent / "cassettes"
GOLDENS = Path(__file__).resolve().parents[1] / "golden" / "routes"


@pytest.mark.parametrize(
    ("metadata", "allowed"),
    [
        ({"licenseId": "CC0_10"}, True),
        ({"licenseId": "PDDL"}, True),
        ({"licenseId": "PUBLIC_DOMAIN"}, True),
        ({"licenseId": "SEE_TERMS_OF_USE"}, False),
        ({"licenseId": "CC_40_BY"}, False),
        ({"licenseId": None}, False),
        ({"editingInfo": {"lastEditDate": 1}}, False),
        (None, False),
    ],
)
def test_only_a_public_domain_dedication_lets_a_portal_payload_be_committed(
    metadata: Any, allowed: bool
) -> None:
    assert (committable(metadata) is None) is allowed


def test_an_arcgis_layer_never_qualifies() -> None:
    """A layer's metadata has no licence id; its item's `licenseInfo` is HTML a test cannot
    read as a grant, so the absence is the answer."""
    reason = committable({"name": "Closures", "editingInfo": {"lastEditDate": 1793000000000}})
    assert reason is not None and "free HTML" in reason


def test_every_committed_portal_payload_is_an_adopted_dataset() -> None:
    """A cassette nobody's adapter reads is somebody's data republished for nothing."""
    adopted = {e.name.replace(".", "_").replace("-", "_") for e in read(CATALOG).adopted}
    for path in CASSETTES.glob("portal_*.json"):
        if path.name.endswith(".meta.json"):
            continue
        assert path.stem in adopted, f"{path.name} names no adopted catalog row"


def test_an_adopted_rows_recorded_licence_is_what_its_metadata_said() -> None:
    """The row's `licence.id` is the id the metadata declared when it was read, so a
    committed `.meta.json` and the row cannot disagree."""
    by_stem = {e.name.replace(".", "_").replace("-", "_"): e for e in read(CATALOG).adopted}
    for meta in CASSETTES.glob("portal_*.meta.json"):
        entry = by_stem.get(meta.name.removesuffix(".meta.json"))
        assert entry is not None, meta.name
        declared = json.loads(meta.read_text(encoding="utf-8")).get("licenseId")
        assert entry.licence.id == declared, meta.name


def _golden_portal_tools() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for cassette in sorted(GOLDENS.glob("*/cache.sqlite")):
        with sqlite3.connect(cassette) as db:
            rows = db.execute(
                "SELECT DISTINCT tool FROM cache WHERE tool LIKE 'adapter.portal.%'"
            ).fetchall()
        found.extend((cassette.parent.name, row[0]) for row in rows)
    return found


def test_a_golden_records_a_portal_only_under_a_committable_licence() -> None:
    entries = {e.name: e for e in read(CATALOG).adopted}
    for route, tool in _golden_portal_tools():
        name = tool.removeprefix("adapter.").removesuffix(".meta")
        entry = entries.get(name)
        assert entry is not None, f"{route} records {tool}, which no catalog row adopts"
        assert entry.licence.id in COMMITTABLE_LICENCES, (
            f"{route} records {name}, whose dataset declares {entry.licence.id!r}; only a "
            f"public-domain dedication may be committed (ADR 0061)"
        )
