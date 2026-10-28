"""Google's encoded polyline format, decoded (M20).

The 511 platforms IBI Group builds for Georgia, Alaska, Nevada, Connecticut and others draw
an event's extent as `EncodedPolyline`: the road it covers, as published, rather than a
chord between two points. That is the difference between a record that can say it runs
*along* a route and one that can only say it is *near* one (ADR 0013 gate 2), so it is
decoded rather than discarded.

Pure and dependency-free. The algorithm is Google's own, and the test oracle is the worked
example on Google's page - three points from 38.5, -120.2 - not this module's output.
https://developers.google.com/maps/documentation/utilities/polylinealgorithm
"""

from __future__ import annotations

#: The format's offset: each 5-bit chunk is stored as a printable character from `?` on.
_OFFSET = 63

#: The continuation bit. A chunk with it set is followed by another for the same number.
_MORE = 0x20


def _numbers(encoded: str) -> list[int]:
    """Every signed integer in the string, in order."""
    out: list[int] = []
    value = shift = 0
    for char in encoded:
        chunk = ord(char) - _OFFSET
        if not 0 <= chunk < 64:
            raise ValueError(f"{char!r} is not a polyline character")
        value |= (chunk & 0x1F) << shift
        shift += 5
        if chunk & _MORE:
            continue
        out.append(~(value >> 1) if value & 1 else value >> 1)
        value = shift = 0
    if shift:
        raise ValueError("the polyline ends in the middle of a number")
    return out


def decode(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """An encoded polyline as `(lon, lat)` pairs - GeoJSON's order, not the format's.

    The format stores latitude first, and every consumer here is GeoJSON, which puts
    longitude first; swapping once, here, is what keeps a route in Georgia from being drawn
    in Antarctica. Raises `ValueError` on a string that is not a whole polyline: a caller
    that wants a partial line has a chord to fall back on, and a truncated one would be a
    shorter road than the publisher drew.
    """
    numbers = _numbers(encoded)
    if len(numbers) % 2:
        raise ValueError("the polyline has a latitude with no longitude")
    scale = 10.0**precision
    lat = lon = 0
    points: list[tuple[float, float]] = []
    for index in range(0, len(numbers), 2):
        lat += numbers[index]
        lon += numbers[index + 1]
        points.append((lon / scale, lat / scale))
    return points


__all__ = ["decode"]
