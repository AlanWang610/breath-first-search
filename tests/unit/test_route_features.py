"""Adapter windows read on the route's clock (ADR 0046).

A feature's window is naive UTC; an ETA is naive local. These pin the conversion in the one
place it is made - `core.data.features.RouteFeatures` - plus the two guards in front of it:
the `Feature` validator that converts an aware stamp, and `local_to_utc` for publishers
that send local wall clocks with no offset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from longrun.adapters.base import local_to_utc
from longrun.core.data.features import RouteFeatures
from longrun.core.models.features import Feature, FeatureSet


def _window(start: datetime | None, end: datetime | None) -> Feature:
    return Feature(
        kind="closures",
        category="road-closure",
        geometry={},
        tier=1,
        confidence=0.9,
        start=start,
        end=end,
    )


def _clock(offset: float | None) -> RouteFeatures:
    return RouteFeatures(found=FeatureSet(), utc_offset_hours=offset)


def test_a_local_eta_is_converted_before_it_meets_a_utc_window() -> None:
    feature = _window(datetime(2026, 9, 15, 12, 0), datetime(2026, 9, 15, 14, 0))
    pdt = _clock(-7.0)
    assert pdt.active_at(feature, datetime(2026, 9, 15, 6, 0)) is True  # 13:00Z
    assert pdt.active_at(feature, datetime(2026, 9, 15, 8, 0)) is False  # 15:00Z


def test_an_unknown_offset_is_unknown_not_open() -> None:
    """Absence is not zero (scope 12). A bounded window read on no clock says nothing."""
    feature = _window(datetime(2026, 9, 15, 12, 0), datetime(2026, 9, 15, 14, 0))
    assert _clock(None).active_at(feature, datetime(2026, 9, 15, 13, 0)) is None
    assert _clock(None).to_utc(datetime(2026, 9, 15, 13, 0)) is None


def test_an_unbounded_record_needs_no_clock() -> None:
    """An alert with no window applies at every instant, whether or not the offset is known."""
    assert _clock(None).active_at(_window(None, None), datetime(2026, 9, 15, 13, 0)) is True


def test_to_local_and_to_utc_are_inverses() -> None:
    clock = _clock(-5.0)
    local = datetime(2026, 9, 15, 7, 30)
    utc = clock.to_utc(local)
    assert utc == datetime(2026, 9, 15, 12, 30)
    assert clock.to_local(utc) == local


def test_the_feature_validator_converts_an_aware_stamp() -> None:
    """Converted, not refused and not stripped - stripping is the bug being fixed."""
    feature = _window(
        datetime(2026, 9, 15, 6, 59, tzinfo=timezone(timedelta(hours=-7))),
        datetime(2026, 9, 15, 20, 0, tzinfo=UTC),
    )
    assert feature.start == datetime(2026, 9, 15, 13, 59)
    assert feature.end == datetime(2026, 9, 15, 20, 0)
    assert feature.start is not None and feature.start.tzinfo is None


def test_local_to_utc_uses_the_offset_in_force_on_each_date() -> None:
    """The feed's zone, per timestamp, so a window across a DST change is right at both
    ends - which one route offset could not be."""
    summer = local_to_utc(datetime(2026, 7, 1, 9, 0), "America/Chicago")
    winter = local_to_utc(datetime(2026, 12, 1, 9, 0), "America/Chicago")
    assert summer == datetime(2026, 7, 1, 14, 0)  # CDT, -5
    assert winter == datetime(2026, 12, 1, 15, 0)  # CST, -6
