"""What may be committed as a recorded payload, by family (ADR 0006, ADR 0038, ADR 0055).

A cassette is somebody's data republished in a public repository, so it needs the publisher's
own redistribution grant, and ADR 0038 accepts only the one shipped inside the payload. Until
M18 the scan that enforced it globbed `wzdx_*.json`, which left the rest of the directory
unguarded: a 511 platform's or a city portal's payload committed as `state511_ga.json` would
have passed every test there was.

Now every file in `cassettes/` must belong to a family named here, and each family states
the grant it accepts. A family with no committable grant - every 511 platform M20 surveys -
is simply not listed, so its payload cannot be committed under any name. What it gets
instead is a shape fixture: `tests/contract/shapes/`, written from documentation with
invented values (`test_shape_fixtures.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

CASSETTES = Path(__file__).parent / "cassettes"

#: The CC0 URL, in both the forms publishers write it. Maricopa omits the trailing slash.
CC0 = "https://creativecommons.org/publicdomain/zero/1.0"


def _wzdx_envelope_is_cc0(payload: Any) -> str | None:
    """WZDx and CWZ carry `feed_info.license` in their own envelope (ADR 0038). A reason
    the file may not be committed, or `None` if it may."""
    info = payload.get("feed_info") or payload.get("road_event_feed_info") or {}
    licence = info.get("license")
    if licence is None:
        return "declares no licence"
    if str(licence).rstrip("/") != CC0:
        return f"declares {licence!r}, not the CC0 dedication ADR 0038 requires"
    return None


def _wzdx_family(path: Path) -> str | None:
    return _wzdx_envelope_is_cc0(json.loads(path.read_text(encoding="utf-8")))


#: Where a portal payload's grant lives: not in the payload - a Socrata row has no envelope -
#: but in the dataset's own metadata, committed verbatim beside it (ADR 0061).
META_SUFFIX = ".meta.json"


def _portal_family(path: Path) -> str | None:
    """A portal payload is committed beside the metadata that granted it, and the grant is a
    public-domain dedication (`catalog.COMMITTABLE_LICENCES`)."""
    from longrun.adapters.portals.catalog import committable

    meta = path.with_name(path.stem + META_SUFFIX)
    if not meta.exists():
        return f"has no verbatim {meta.name} beside it"
    return committable(json.loads(meta.read_text(encoding="utf-8")))


#: Cassette file prefix -> the check its file must pass. A prefix not listed here cannot be
#: committed at all - which is the point.
FAMILIES: dict[str, Callable[[Path], str | None]] = {
    "wzdx_": _wzdx_family,
    "portal_": _portal_family,
}


def _family(name: str) -> Callable[[Path], str | None] | None:
    return next((check for prefix, check in FAMILIES.items() if name.startswith(prefix)), None)


#: Every payload in the directory; a dotfile (`.gitkeep`) is the directory's, not a payload,
#: and a `.meta.json` is the metadata that grants one, checked with it.
PAYLOADS = sorted(
    p
    for p in CASSETTES.iterdir()
    if not p.name.startswith(".") and not p.name.endswith(META_SUFFIX)
)


@pytest.mark.parametrize("path", PAYLOADS, ids=lambda p: p.name if isinstance(p, Path) else str(p))
def test_every_committed_payload_carries_its_familys_grant(path: Path) -> None:
    """The directory, not a list: a list can be extended without thinking; a file cannot be
    added here without this test reading it."""
    check = _family(path.name)
    assert check is not None, (
        f"{path.name} belongs to no family in FAMILIES; a payload with no declared grant "
        f"is a shape fixture (tests/contract/shapes/), not a cassette"
    )
    reason = check(path)
    assert reason is None, f"{path.name} {reason} and must not be committed"


def test_every_metadata_file_grants_a_payload_beside_it() -> None:
    """A `.meta.json` with no payload is metadata nobody reads - most likely a payload that
    was renamed and left its grant behind."""
    for meta in CASSETTES.glob(f"*{META_SUFFIX}"):
        payload = meta.with_name(meta.name.removesuffix(META_SUFFIX) + ".json")
        assert payload.exists(), f"{meta.name} grants nothing: no {payload.name}"


def test_an_unknown_family_is_refused_rather_than_waved_through() -> None:
    """M21 added the portal family. Its grant is read from the dataset's metadata, so the
    prefix alone admits nothing (`test_portal_licences.py`)."""
    assert _family("state511_ga.json") is None
    assert _family("arcgis_kcmo.json") is None
    assert _family("portal_socrata_kcmo.json") is _portal_family
    assert _family("wzdx_modot.json") is _wzdx_family


@pytest.mark.parametrize(
    ("envelope", "allowed"),
    [
        ({"feed_info": {"license": "https://creativecommons.org/publicdomain/zero/1.0/"}}, True),
        ({"road_event_feed_info": {"license": CC0}}, True),
        ({"feed_info": {"license": "https://example.com/terms"}}, False),
        ({"feed_info": {}}, False),
        ({}, False),
    ],
)
def test_the_wzdx_family_accepts_only_cc0_in_the_envelope(
    envelope: dict[str, Any], allowed: bool
) -> None:
    assert (_wzdx_envelope_is_cc0(envelope) is None) is allowed
