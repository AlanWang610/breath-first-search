"""Google's encoded polyline format (M20): the oracle is Google's own worked example."""

from __future__ import annotations

import pytest

from longrun.adapters.state511.polyline import decode


def test_googles_documented_example_decodes_to_its_documented_points() -> None:
    """https://developers.google.com/maps/documentation/utilities/polylinealgorithm"""
    points = decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert points == pytest.approx([(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)])


def test_points_come_out_longitude_first_for_geojson() -> None:
    lon, lat = decode("_p~iF~ps|U")[0]
    assert (lon, lat) == pytest.approx((-120.2, 38.5))


@pytest.mark.parametrize(
    ("encoded", "one_end", "other_end"),
    [
        ("}}}nEzdfgOJAdQo@xLc@~G[pCK|DMhBIf@C", (-85.232752, 34.063612), (-85.23352, 34.07344)),
        (
            "q_s}DreouNYa@uGyIoBmC]c@oAeBcDeE{CuDkB}B_AiAKMKM[_@mA}Ao@w@sAaBMMuAcBy@eAs@}@yBqCwBoCcDiEkA}A",
            (-82.330672, 31.23211),
            (-82.318784, 31.24198),
        ),
    ],
)
def test_the_511ga_documentation_samples_run_between_their_events_two_points(
    encoded: str, one_end: tuple[float, float], other_end: tuple[float, float]
) -> None:
    """511GA's own sample events print `Latitude`/`Longitude` and the `Secondary` pair beside
    each polyline. The line runs between them - in either direction, the two samples
    disagree on which - and the two halves of one document agreeing is the format read
    right. Within 0.001 degrees, about 100 m: the published points are rounded differently."""
    points = decode(encoded)
    ends = sorted([points[0], points[-1]])
    for got, published in zip(ends, sorted([one_end, other_end]), strict=True):
        assert got == pytest.approx(published, abs=1e-3)


def test_precision_six_is_a_tenth_of_precision_five() -> None:
    assert decode("_p~iF~ps|U", precision=6)[0] == pytest.approx((-12.02, 3.85))


def test_an_empty_polyline_is_no_points() -> None:
    assert decode("") == []


@pytest.mark.parametrize(
    "broken",
    [
        "_p~iF~ps|",  # stops in the middle of the longitude
        "_p~iF",  # a latitude with no longitude
        "_p~iF\n~ps|U",  # a character outside the format's range
    ],
)
def test_a_broken_polyline_is_refused_rather_than_shortened(broken: str) -> None:
    with pytest.raises(ValueError):
        decode(broken)
