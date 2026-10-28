"""The keyed feeds M19 adopted, whose payloads declare CC0 (ADR 0038, ADR 0057).

A key decides who can *fetch* a feed, and the envelope's licence decides who may
*republish* what was fetched. These feeds need a key and declare CC0, so each backs a
cassette - recorded by the person holding the key - and that cassette replays in keyless CI
through the adapter itself, which is the path a plan takes (`client.replay_or_refuse`).

`test_keyed_adapters.py` holds every keyed adapter to the shape of a refusal; this file
holds these to their recorded payloads. The live checks are `network`-marked and skip
without the key.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from longrun.adapters.base import AdapterContext
from longrun.adapters.keys import ORDOT
from longrun.adapters.wzdx import ordot
from longrun.adapters.wzdx.client import wzdx_args
from longrun.adapters.wzdx.feed import feed_publisher, feed_version
from longrun.core.data.cache import SqliteCache, args_hash, peek
from longrun.core.models.context import Budget

pytestmark = pytest.mark.contract

CASSETTES = Path(__file__).parent / "cassettes"
DAY = date(2026, 10, 28)

#: The CC0 URL, in both the forms publishers write it.
CC0 = "https://creativecommons.org/publicdomain/zero/1.0"

#: (short, cassette, spec version, publisher, adapter, key).
KEYED_CC0 = [
    ("ordot", "wzdx_ordot.json", "4.0", "Oregon-DOT", ordot.CLOSURES, ORDOT),
]


def _payload(name: str) -> dict[str, Any]:
    return json.loads((CASSETTES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("short", "cassette", "version", "publisher", "adapter", "key"),
    KEYED_CC0,
    ids=[k[0] for k in KEYED_CC0],
)
def test_a_keyed_cc0_feed_replays_through_its_adapter_with_no_key(
    short: str, cassette: str, version: str, publisher: str, adapter: Any, key: Any
) -> None:
    payload = _payload(cassette)
    assert feed_version(payload) == version
    assert feed_publisher(payload) == publisher
    with SqliteCache() as cache:
        cache.put(
            f"adapter.{adapter.name}",
            args_hash(wzdx_args(adapter.name, DAY)),
            DAY.isoformat(),
            payload,
        )
        ctx = AdapterContext(cache=cache, budget=Budget(), offline=True)
        result = adapter.fetch(None, DAY, ctx)
    assert result.answered and not result.key_missing, result.reason
    assert len(result.features) == len(payload["features"])
    assert all(f.tier == 1 and f.kind == "closures" and f.detail for f in result.features)
    assert ctx.budget.api_calls_used == 0


@pytest.mark.network
@pytest.mark.parametrize(
    ("short", "cassette", "version", "publisher", "adapter", "key"),
    KEYED_CC0,
    ids=[k[0] for k in KEYED_CC0],
)
def test_a_keyed_feed_still_declares_the_licence_its_cassette_was_committed_under(
    short: str, cassette: str, version: str, publisher: str, adapter: Any, key: Any
) -> None:
    """Fetched through the adapter, so the check authenticates exactly as a plan does; the
    raw payload is then read back out of the cache it was written to."""
    import os

    if not key.value(dict(os.environ)):
        pytest.skip(f"{key.env_var} is not set")
    today = date.today()
    with SqliteCache() as cache:
        result = adapter.fetch(None, today, AdapterContext(cache=cache, budget=Budget()))
        assert result.answered, result.reason
        payload = peek(cache, f"adapter.{adapter.name}", wzdx_args(adapter.name, today), today)
    info = payload.get("feed_info") or payload.get("road_event_feed_info") or {}
    licence = str(info.get("license") or "")
    assert licence.rstrip("/") == CC0, f"{short} now declares {licence!r}; do not re-record"
