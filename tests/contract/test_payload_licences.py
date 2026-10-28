"""What may be committed as a recorded payload, by family (ADR 0006, ADR 0038, ADR 0055).

A cassette is somebody's data republished in a public repository, so it needs the publisher's
own redistribution grant, and ADR 0038 accepts only the one shipped inside the payload. Until
M18 the scan that enforced it globbed `wzdx_*.json`, which left the rest of the directory
unguarded: a 511 platform's or a city portal's payload committed as `state511_ga.json` would
have passed every test there was.

Now every file in `cassettes/` must belong to a family named here, and each family states
the grant it accepts. A family with no committable grant - every IBI 511 platform M20
surveys - is simply not listed, so its payload cannot be committed under any name. What it
gets instead is a shape fixture: `tests/contract/shapes/`, written from documentation with
invented values (`test_shape_fixtures.py`).

One family is granted by a terms page rather than by anything it carries: Caltrans' Lane
Closure System, under Caltrans' conditions of use. That is an exception to ADR 0038, the
user's call, made 2026-10-29, and ADR 0059 records it.
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


#: Caltrans' grant, which is in neither the payload nor a dataset's metadata but on the
#: publisher's terms page - the one exception to ADR 0038's payload-only rule, and the
#: user's call, made 2026-10-29 (ADR 0059, "Caltrans' conditions of use are its grant").
#: CWWP2's every page links these conditions as its own. Quoted verbatim from the page as
#: read on 2026-10-29 (the page is dated July 19, 2021); `test_state511.py` re-reads it live.
CALTRANS_TERMS = {
    "url": "https://dot.ca.gov/conditions-of-use",
    "read_on": "2026-10-29",
    "quote": (
        "In general, information presented on this website, unless otherwise indicated, is "
        "considered in the public domain. It may be distributed or copied as permitted by law."
    ),
}

#: Words that would be an indication otherwise. The grant holds "unless otherwise
#: indicated", so a Caltrans payload that names a licence or a copyright of its own is
#: governed by that instead, and is not committed on the strength of the terms page.
_OTHERWISE = ("licen", "copyright", "terms", "rights")


def _caltrans_family(path: Path) -> str | None:
    """A Caltrans Lane Closure System file, granted by Caltrans' conditions of use rather
    than by anything inside it - provided it is that file and indicates nothing otherwise."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not all(
        isinstance(r, dict) and isinstance(r.get("lcs"), dict) for r in records
    ):
        return "is not a Caltrans Lane Closure System file, which is all the terms page grants"

    def keys(node: Any) -> list[str]:
        if isinstance(node, dict):
            return [k for key, value in node.items() for k in (key, *keys(value))]
        if isinstance(node, list):
            return [k for item in node for k in keys(item)]
        return []

    otherwise = sorted({k for k in keys(payload) if any(w in k.lower() for w in _OTHERWISE)})
    if otherwise:
        return f"indicates otherwise ({', '.join(otherwise)}), which the grant defers to"
    return None


#: Cassette file prefix -> the check its file must pass. A prefix not listed here cannot be
#: committed at all - which is the point.
FAMILIES: dict[str, Callable[[Path], str | None]] = {
    "wzdx_": _wzdx_family,
    "portal_": _portal_family,
    "state511_caltrans_": _caltrans_family,
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
    prefix alone admits nothing (`test_portal_licences.py`). M20 added Caltrans by name: an
    IBI 511 platform's payload is still refused under any `state511_` name."""
    assert _family("state511_ga.json") is None
    assert _family("state511_caltrans_d4.json") is _caltrans_family
    assert _family("arcgis_kcmo.json") is None
    assert _family("portal_socrata_kcmo.json") is _portal_family
    assert _family("wzdx_modot.json") is _wzdx_family


@pytest.mark.parametrize(
    ("payload", "allowed"),
    [
        ({"data": [{"lcs": {"index": "x", "closure": {"facility": "Mainline"}}}]}, True),
        ({"data": []}, True),
        ({"data": [{"lcs": {"index": "x"}, "license": "CC-BY"}]}, False),
        ({"copyright": "someone else", "data": [{"lcs": {}}]}, False),
        ({"data": [{"ID": 1, "EventType": "roadwork"}]}, False),
        ([{"ID": 1}], False),
    ],
)
def test_the_caltrans_family_admits_only_an_lcs_file_that_indicates_nothing_otherwise(
    tmp_path: Path, payload: Any, allowed: bool
) -> None:
    """The grant is "unless otherwise indicated", so a file naming a licence or a copyright
    of its own is not committed on the terms page's strength - and it is a grant for
    Caltrans' Lane Closure System, not for anything filed under its name."""
    path = tmp_path / "state511_caltrans_x.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert (_caltrans_family(path) is None) is allowed


@pytest.mark.network
def test_caltrans_still_says_what_its_recorded_files_are_committed_under() -> None:
    """The Caltrans cassettes stand on one sentence of a terms page (ADR 0059). If it is
    reworded or withdrawn this fails, and the recordings are a decision to revisit."""
    import re

    import httpx

    page = httpx.get(
        CALTRANS_TERMS["url"], timeout=60, follow_redirects=True, headers={"User-Agent": "longrun"}
    )
    page.raise_for_status()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.text))
    assert CALTRANS_TERMS["quote"] in text


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
