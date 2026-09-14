"""USGS The National Map, asked for real (ADR 0023). `network`-marked, so never in CI.

The unit tests pin the two facts that shaped `core/data/tiles.py` - the `{z}/{y}/{x}` order
and the zoom-16 ceiling - against numbers written down on 2026-09-14. These check the facts
are still true of the service, which is the only thing that can drift underneath a
committed constant without any code changing.
"""

from __future__ import annotations

import pytest

from longrun.core.data.cache import SqliteCache
from longrun.core.data.tiles import USGS_IMAGERY, USGS_TOPO, fetch_tile
from longrun.core.models.context import Budget

pytestmark = pytest.mark.network

DE_YOUNG = (37.7715, -122.4686)


@pytest.mark.parametrize("provider", [USGS_IMAGERY, USGS_TOPO], ids=lambda p: p.id)
def test_a_real_tile_comes_back_at_the_measured_ceiling(provider: object) -> None:
    with SqliteCache() as cache:
        tile = fetch_tile(provider, *DE_YOUNG, 16, cache=cache, budget=Budget())  # type: ignore[arg-type]

    assert tile.found, tile.reason
    assert (tile.media_type or "").startswith("image/")
    assert len(tile.data or b"") > 2000, "a real tile is tens of kilobytes, not an error page"


def test_the_ceiling_is_still_sixteen() -> None:
    """If USGS starts serving 17, `maxzoom` is leaving detail on the table - and this is the
    test that would say so. Asked directly, bypassing the clamp."""
    import httpx

    x17, y17 = 20946, 50663
    response = httpx.get(USGS_IMAGERY.url(17, x17, y17), timeout=30)

    assert response.status_code == 404


def test_outside_the_us_is_no_tile_rather_than_an_error() -> None:
    with SqliteCache() as cache:
        tile = fetch_tile(USGS_IMAGERY, 30.0, -150.0, 12, cache=cache, budget=Budget())

    assert not tile.found
    assert "no tile at this location" in (tile.reason or "")
