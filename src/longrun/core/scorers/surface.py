"""Surface composition and shoulder exposure (scope 7.2, 12).

Two measurements, one shape. Both are *sustained runs*: a 40 m patch of gravel between two
paved blocks is not a surface problem, and 40 m of shoulder is not a shoulder problem. Only
a run that persists past a threshold gets a flag, and the flag lands on the segment where
the run begins — one flag per run, not one per segment in it, so that a 2 km gravel section
does not out-weigh a genuinely worse 300 m one just by being subdivided into more segments.

**Surface is scored symmetrically against the profile.** `surface=paved` flags sustained
unpaved; `surface=dirt` flags sustained paved; `mixed` — the shipped default — flags
neither and only reports the fractions. That symmetry is scope 3.2 taken literally: the
scorer holds no opinion about dirt, it only reports where the route crosses the line the
user drew.

**Shoulder running is flagged only where it is taggable** (scope 7.2's own wording). The
condition is a road with a known-absent sidewalk *and* a known-absent shoulder: both facts
have to be positively tagged. An untagged rural road is the overwhelmingly common case in
US OSM data (scope 12), and reading its silence as "no sidewalk, no shoulder" would flag
most of the country's road mileage while telling the user nothing they can act on.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.routing.lts import SEPARATED_HIGHWAYS, has_sidewalk
from longrun.core.scorers._common import (
    ROUTE_SUMMARY_ID,
    UNKNOWN_WAY_CONFIDENCE,
    WAYS_LAYER,
    segment_tags,
    way_tags_in_corridor,
)
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "surface_profile"

PAVED_SURFACES = frozenset(
    {
        "asphalt",
        "chipseal",
        "cobblestone",
        "concrete",
        "concrete:lanes",
        "concrete:plates",
        "metal",
        "paved",
        "paving_stones",
        "rubber",
        "sett",
        "wood",
    }
)

UNPAVED_SURFACES = frozenset(
    {
        "compacted",
        "dirt",
        "earth",
        "fine_gravel",
        "grass",
        "gravel",
        "ground",
        "mud",
        "pebblestone",
        "rock",
        "sand",
        "stone",
        "unpaved",
        "woodchips",
    }
)

#: A run of unpaved (or paved) surface long enough to be a property of the route rather
#: than of one driveway apron.
SUSTAINED_SURFACE_M = 500.0

#: Shoulder running is nastier per metre than gravel is, so it needs less of it.
SUSTAINED_SHOULDER_M = 300.0

#: Tag values meaning the shoulder is positively absent.
NO_SHOULDER_VALUES = frozenset({"no", "none", "0"})

#: Which surface class each profile preference objects to.
FLAGGED_CLASS: dict[str, str] = {"paved": "unpaved", "dirt": "paved"}

REASON_BY_CLASS: dict[str, str] = {
    "unpaved": "sustained_unpaved",
    "paved": "sustained_paved",
}

#: Confidence for a segment whose way is known but whose surface is not.
UNKNOWN_SURFACE_CONFIDENCE = 0.5


def surface_class(tags: dict[str, Any]) -> str | None:
    """`"paved"`, `"unpaved"`, or None for unknown (scope 12).

    None is a real answer and is what the unknown fraction in the summary is built from.
    An untagged footway is not a paved footway; a great many of them are dirt.
    """
    value = tags.get("surface")
    if value is not None:
        text = str(value).strip().lower()
        if text in PAVED_SURFACES:
            return "paved"
        if text in UNPAVED_SURFACES:
            return "unpaved"
        return None
    # `tracktype` grades an unpaved track's firmness; its presence is itself the evidence.
    if tags.get("tracktype") is not None:
        return "unpaved"
    return None


def has_shoulder(tags: dict[str, Any]) -> bool | None:
    """True, False, or None for unknown — the same three-way answer as `has_sidewalk`."""
    for key in ("shoulder", "shoulder:both"):
        value = tags.get(key)
        if value is None:
            continue
        return str(value).strip().lower() not in NO_SHOULDER_VALUES
    if tags.get("shoulder:left") is not None or tags.get("shoulder:right") is not None:
        return True
    return None


def on_shoulder(tags: dict[str, Any]) -> bool | None:
    """Whether the runner is in the roadway of a road with no shoulder.

    None whenever either fact is untagged. This is the "where taggable" clause of scope
    7.2, and it is the difference between a useful flag and a national false positive.
    """
    highway = str(tags.get("highway", "")).strip().lower()
    if not highway or highway in SEPARATED_HIGHWAYS:
        return False
    sidewalk, shoulder = has_sidewalk(tags), has_shoulder(tags)
    if sidewalk is None or shoulder is None:
        return None
    return sidewalk is False and shoulder is False


def runs(
    segments: Iterable[Segment], classes: dict[str, Any], wanted: Any
) -> list[tuple[Segment, float]]:
    """Maximal consecutive runs matching `wanted`, as `(first segment, run length)`."""
    found: list[tuple[Segment, float]] = []
    start: Segment | None = None
    length = 0.0
    for segment in segments:
        if classes.get(segment.id) == wanted:
            start = start or segment
            length += segment.length_m
        elif start is not None:
            found.append((start, length))
            start, length = None, 0.0
    if start is not None:
        found.append((start, length))
    return found


def _severity(run_m: float, threshold_m: float) -> float:
    """0.5 at the threshold, 1.0 at twice it. A longer run is a worse one."""
    return min(1.0, run_m / (2.0 * threshold_m))


def surface_profile(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Fraction by surface type, with sustained-run and shoulder flags (scope 7.2)."""
    try:
        by_way = way_tags_in_corridor(route, ctx)
    except LayerNotFound as exc:
        return unavailable(name, f"ways layer unavailable: {exc}")

    result = ScorerResult(name=name)
    classes: dict[str, str | None] = {}
    shoulders: dict[str, bool | None] = {}
    metres: dict[str, float] = {"paved": 0.0, "unpaved": 0.0, "unknown": 0.0}

    for segment in segments:
        tags = segment_tags(segment, by_way)
        if tags is None:
            classes[segment.id] = None
            shoulders[segment.id] = None
            metres["unknown"] += segment.length_m
            result.measurements.append(
                SegmentMeasurement(
                    segment_id=segment.id,
                    values={"surface": None, "surface_class": None, "on_shoulder": None},
                    confidence=UNKNOWN_WAY_CONFIDENCE,
                )
            )
            continue

        klass = surface_class(tags)
        shoulder = on_shoulder(tags)
        classes[segment.id] = klass
        shoulders[segment.id] = shoulder
        metres[klass or "unknown"] += segment.length_m

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "surface": tags.get("surface"),
                    "surface_class": klass,
                    "sidewalk": has_sidewalk(tags),
                    "shoulder": has_shoulder(tags),
                    "on_shoulder": shoulder,
                },
                confidence=1.0 if klass is not None else UNKNOWN_SURFACE_CONFIDENCE,
            )
        )

    preference = str(ctx.profile.surface.value)
    flagged_class = FLAGGED_CLASS.get(preference)
    if flagged_class is not None:
        for first, run_m in runs(segments, classes, flagged_class):
            if run_m < SUSTAINED_SURFACE_M:
                continue
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=first.id,
                    kind=FlagKind.SOFT,
                    tier=Tier.COMFORT,
                    severity=_severity(run_m, SUSTAINED_SURFACE_M),
                    reason_code=REASON_BY_CLASS[flagged_class],
                    detail=(
                        f"{run_m:.0f} m of {flagged_class} from {first.cum_start_m / 1000:.2f} km, "
                        f"against a stated surface preference of {preference}"
                    ),
                )
            )

    for first, run_m in runs(segments, shoulders, True):
        if run_m < SUSTAINED_SHOULDER_M:
            continue
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=first.id,
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=_severity(run_m, SUSTAINED_SHOULDER_M),
                reason_code="sustained_shoulder_running",
                detail=(
                    f"{run_m:.0f} m in the roadway from {first.cum_start_m / 1000:.2f} km: "
                    f"no sidewalk and no shoulder"
                ),
            )
        )

    total = sum(metres.values())
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "paved_fraction": (metres["paved"] / total) if total else None,
                "unpaved_fraction": (metres["unpaved"] / total) if total else None,
                "unknown_fraction": (metres["unknown"] / total) if total else None,
                "paved_m": metres["paved"],
                "unpaved_m": metres["unpaved"],
                "unknown_m": metres["unknown"],
                "surface_preference": preference,
            },
            confidence=1.0 - (metres["unknown"] / total if total else 1.0),
        )
    )

    record_coverage(
        result, source=WAYS_LAYER, kind="osm_surface", vintage=ctx.layers.vintage(WAYS_LAYER)
    )
    return result


#: The name the scorer registry loads. Aliased so a caller can dispatch on
#: `module.score` without knowing which scope tool this module implements.
score = surface_profile
