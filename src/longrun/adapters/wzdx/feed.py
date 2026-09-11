"""Reading a WZDx feed, whatever version it is (scope 7.6, 7.10).

Pure: GeoJSON in, `Feature` out. No HTTP, no cache, no jurisdiction - all of which live in
the adapters that use it, so the part most likely to break when a publisher upgrades is the
part that can be tested against a recorded payload with no network at all.

**Three spec versions are in play across four feeds**, and this was measured rather than
assumed by reading the USDOT feed registry and then fetching each one on 2026-09-10:
Kansas DOT publishes 4.0, Missouri DOT 4.1, Maricopa County 4.2. The registry itself is
stale - the Kansas URL it lists 301-redirects to a different host - which is what "tiers 3
and 4 are brittle by design" looks like arriving one tier early.

**The drift that actually bit is the envelope key.** 4.1 renamed `road_event_feed_info` to
`feed_info`; Kansas still publishes the old one, so a reader that knew only the new name
declared a live 480-feature feed to have no envelope and reported a whole state unavailable.
`ENVELOPE_KEYS` looks for both.

An earlier version of this docstring claimed that 4.0 flattens onto `properties` what later
versions nest under `core_details`. That is **wrong** - `core_details` was introduced in 4.0
and Kansas nests exactly like Missouri - and the test written to demonstrate it is what
found the error. `_detail` still looks in both places, which costs nothing and covers a
publisher that flattens, but it is defensive rather than load-bearing and should not be
described as the reason one parser serves three versions.

**Nothing here raises on a malformed feature.** A feed that changes shape mid-release drops
the features it broke and keeps the ones it did not, because the alternative is a route
losing every closure in a state over one bad record.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.coverage import Tier
    from longrun.core.models.features import Feature, FeatureKind

#: WZDx `vehicle_impact` values carried through onto `Feature.category`, which is what
#: `closures.blocks_pedestrians` reads. Passed through verbatim rather than mapped, so a new
#: value in a future spec reaches the scorer as itself rather than as a guess.
IMPACT_FIELD = "vehicle_impact"

#: `vehicle_impact` values that say nothing, so the category falls back to the event type.
#:
#: **Maricopa County publishes `"unknown"` for all 2,628 of its features**, which is not a
#: parsing problem and not something to work around - it is the publisher declining to state
#: the impact. The consequence is worth being explicit about: no Maricopa work zone can clear
#: ADR 0013's gate 1 on this field, so Phoenix closures are reported and never disqualifying
#: unless the *description* names a sidewalk. Honest, and a smaller claim than the gate was
#: written expecting.
UNINFORMATIVE = frozenset({"unknown", "none", ""})

#: What a WZDx feed is worth. Tier 1 by scope 7.10's own list, and these are published by
#: the DOT that owns the roadworks - as structured as this data ever gets.
WZDX_TIER: Tier = 1

#: A structured feed's confidence. Clears `closures.HARD_FLAG_CONFIDENCE` (0.8), which is
#: the point: a WZDx `all-lanes-closed` running along the route is what check 6 exists for.
#:
#: Not 1.0, and the missing 0.05 is real. WZDx describes *vehicle* impact - the spec has no
#: pedestrian field at all - so "the road is shut" is an inference about the footway beside
#: it, however good a one.
WZDX_CONFIDENCE = 0.95


def _detail(properties: dict[str, Any], key: str) -> Any:
    """One field, from wherever this spec version put it.

    4.1 introduced `core_details` and moved `event_type`, `road_names`, `direction` and
    `description` into it. 4.0 has them flat on `properties`. Looking in both is what lets
    one parser read Kansas (4.0), Missouri (4.1) and Maricopa (4.2).
    """
    core = properties.get("core_details")
    if isinstance(core, dict) and key in core:
        return core[key]
    return properties.get(key)


def parse_timestamp(value: Any) -> datetime | None:
    """A WZDx timestamp, or `None`.

    Feeds emit RFC 3339 with a `Z`, with a numeric offset, and - Maricopa - with seven
    fractional digits, which `fromisoformat` rejected before 3.11 and accepts now. A
    timestamp that will not parse becomes `None`, which `Feature.active_at` treats as
    unbounded: a closure whose end date is unreadable is one nobody can say has lifted.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Naive throughout `core/`: ETAs are naive local times and comparing them against an
    # aware datetime raises. The offset is discarded rather than converted, which is the
    # same simplification `forecast.py` makes for hourly series.
    return parsed.replace(tzinfo=None)


def _category(properties: dict[str, Any]) -> str:
    """The category `closures.blocks_pedestrians` tests.

    `vehicle_impact` where the feed has it, because that is the field that says whether the
    way is shut; the event type otherwise, so a feed missing the impact still classifies as
    something rather than as an empty string.
    """
    impact = properties.get(IMPACT_FIELD)
    if isinstance(impact, str) and impact.strip() and impact.strip().lower() not in UNINFORMATIVE:
        return impact.strip().lower()
    event = _detail(properties, "event_type")
    return str(event).strip().lower() if event else "work-zone"


def _describe(properties: dict[str, Any]) -> str | None:
    """Prose for the flag detail, and the fallback `blocks_pedestrians` reads for a sidewalk
    closure that the schema has no field for."""
    parts: list[str] = []
    roads = _detail(properties, "road_names")
    if isinstance(roads, list) and roads:
        parts.append(", ".join(str(r) for r in roads))
    elif isinstance(roads, str) and roads.strip():
        parts.append(roads.strip())
    description = _detail(properties, "description")
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    return " - ".join(parts) if parts else None


#: Where the envelope lives, newest name first. 4.1 renamed `road_event_feed_info` to
#: `feed_info`, and Kansas still publishes the old one - so a reader that knew only the new
#: name declared a live 480-feature feed to have "no WZDx envelope". Found against the real
#: feed, not against the spec.
ENVELOPE_KEYS = ("feed_info", "road_event_feed_info")


def _envelope(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ENVELOPE_KEYS:
        info = payload.get(key)
        if isinstance(info, dict):
            return info
    return None


def feed_version(payload: dict[str, Any]) -> str | None:
    """The spec version a feed declares, for the manifest's vintage."""
    info = _envelope(payload)
    if info is None:
        return None
    version = info.get("version")
    return str(version) if version is not None else None


def feed_publisher(payload: dict[str, Any]) -> str | None:
    info = _envelope(payload)
    if info is None:
        return None
    publisher = info.get("publisher")
    return str(publisher) if publisher is not None else None


def parse_wzdx(
    payload: Any,
    *,
    kind: FeatureKind = "closures",
    jurisdiction: str | None = None,
    source_url: str | None = None,
) -> list[Feature]:
    """Every work zone in a feed, as `Feature`s. Never raises.

    A payload that is not a FeatureCollection returns nothing rather than raising: a 200
    carrying an HTML error page is a real thing feeds do, and the caller turns an empty
    result into a coverage reason that says the feed was read and had nothing - which is
    almost right, and much better than a traceback out of `longrun repair`.
    """
    from longrun.core.models.features import Feature

    if not isinstance(payload, dict):
        return []
    features = payload.get("features")
    if not isinstance(features, list):
        return []

    out: list[Feature] = []
    for entry in features:
        if not isinstance(entry, dict):
            continue
        geometry = entry.get("geometry")
        properties = entry.get("properties")
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            continue
        try:
            out.append(
                Feature(
                    kind=kind,
                    category=_category(properties),
                    geometry=geometry,
                    tier=WZDX_TIER,
                    confidence=WZDX_CONFIDENCE,
                    jurisdiction=jurisdiction,
                    start=parse_timestamp(properties.get("start_date")),
                    end=parse_timestamp(properties.get("end_date")),
                    source_url=source_url,
                    detail=_describe(properties),
                )
            )
        except ValueError:
            # `Feature` rejects an end before its start, and feeds do publish those. One bad
            # record must not cost a route every closure in the state.
            continue
    return out


__all__ = [
    "IMPACT_FIELD",
    "WZDX_CONFIDENCE",
    "WZDX_TIER",
    "feed_publisher",
    "feed_version",
    "parse_timestamp",
    "parse_wzdx",
]
