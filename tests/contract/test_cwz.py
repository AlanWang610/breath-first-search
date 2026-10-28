"""CWZ 1.0, read through the WZDx reader (ADR 0058, superseding ADR 0039).

ADR 0039 declined to parse Connected Work Zones because no reachable feed would show a
single record: a reader written from the specification alone would be tested against
nothing, and parsing to nothing reads exactly like a state with no work zones. M19 held
keys for two CWZ feeds, called both, and found the existing reader takes every record.

So this file pins the evidence rather than a second parser: every recorded CWZ payload
parses in full, the fields gate 1 reads survive, and a CWZ feed that stops parsing says
so as CWZ.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from longrun.adapters.base import AdapterContext
from longrun.adapters.wzdx.client import fetch_feed
from longrun.adapters.wzdx.feed import feed_version, parse_wzdx
from longrun.core.data.cache import SqliteCache
from longrun.core.models.context import Budget

pytestmark = pytest.mark.contract

CASSETTES = Path(__file__).parent / "cassettes"
RECORDED = sorted(CASSETTES.glob("cwz_*.json"))

#: The one keyless CWZ feed, CC0 and empty every time it has been called.
PURPOSEBUILT = "https://feeds.purposebuilt.systems/cwz"


def _payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_there_is_a_recorded_cwz_payload_to_test_against() -> None:
    """The whole of ADR 0039's objection, answered: a payload with records in it."""
    assert RECORDED


@pytest.mark.parametrize("path", RECORDED, ids=[p.stem for p in RECORDED])
def test_every_recorded_cwz_payload_parses_in_full(path: Path) -> None:
    payload = _payload(path)
    assert feed_version(payload) == "1.0"
    features = parse_wzdx(payload, source_url="cwz")
    assert len(features) == len(payload["features"]), "a CWZ record the reader dropped"
    assert all(f.start is not None and f.end is not None for f in features)
    assert all(f.detail for f in features)


def test_a_cwz_closure_still_reaches_gate_one() -> None:
    """CWZ adds per-lane status and worker presence; the reader ignores both, and gate 1
    reads `vehicle_impact`, which CWZ keeps."""
    from longrun.core.scorers.closures import blocks_pedestrians

    closed = [
        f
        for path in RECORDED
        for f in parse_wzdx(_payload(path))
        if f.category == "all-lanes-closed"
    ]
    assert closed, "no recorded CWZ payload carries a fully closed record"
    assert all(blocks_pedestrians(f) for f in closed)


@respx.mock
def test_a_cwz_feed_that_stops_parsing_says_so_as_cwz() -> None:
    url = "https://cwz.example.invalid/feed"
    unreadable = {
        "type": "FeatureCollection",
        "feed_info": {"version": "1.0", "publisher": "somebody"},
        "features": [{"type": "Feature", "properties": None, "geometry": None}],
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=unreadable))
    with SqliteCache() as cache:
        ctx = AdapterContext(cache=cache, budget=Budget())
        result = fetch_feed(url, "wzdx.example", date(2026, 10, 28), ctx, spec="cwz")
    assert result.reason == "the feed carried 1 record(s) and none read as CWZ"
    assert result.vintage == "cwz-1.0"


@pytest.mark.network
def test_purposebuilt_still_answers_as_cwz() -> None:
    """Keyless and CC0, so one record in it would be a committable CWZ cassette with no key
    involved (ADR 0039's second way back). Empty on 2026-09-23 and 2026-10-28."""
    try:
        response = httpx.get(PURPOSEBUILT, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - an unreachable feed is a skip
        pytest.skip(f"PurposeBuilt unreachable: {exc}")
    assert feed_version(payload) == "1.0"
    assert len(parse_wzdx(payload)) == len(payload.get("features") or [])
