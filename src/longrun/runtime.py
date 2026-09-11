"""The composition root: where a `ScorerContext` is built out of the environment.

Above `core/` for one reason and it is the reason ADR 0014 gives: this constructs an
`AdapterRegistry`, and `core/` declares a `FeatureSource` Protocol rather than importing the
registry that satisfies it. Beside `regions/` rather than inside `cli/` for another: the
agent loop, the MCP tool layer and the job runner all need the same context, and none of
them may import a CLI command.

**One plan is one context**, and therefore one `Budget`, one `SqliteCache` and one
`CoverageManifest`. That is a change of meaning rather than of shape: until M5.1 the context
was built inside the function that scored a single route, so `longrun plan --alternatives 3`
built four of them and ran four independent 200-call budgets against a scope 6.4 cap of 200.
Scope 8.1 step 6 scores several candidates per round for up to five rounds, which would have
made that sixteen.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from longrun.core.data.cache import SqliteCache, cache_path_from_env
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.plan import SnapshotPins
from longrun.core.models.profile import PreferenceProfile


@contextmanager
def open_context(
    *,
    route: Any,
    root: Path,
    snapshot: SnapshotPins,
    start_at: datetime,
    profile: PreferenceProfile,
    offline: bool = False,
    cache_path: Path | None = None,
    remote_rasters: bool = False,
    utc_offset: float | None = None,
) -> Iterator[ScorerContext]:
    """Open the one context a plan is scored against, and close its cache afterwards.

    Held open with `with`: the SQLite connection is otherwise left to the garbage collector,
    and on Windows an unclosed handle keeps a file lock, so a leaked one can stop the next
    run - or a test's `tmp_path` cleanup - from removing the file.
    """
    # Precedence: an explicit path, then LONGRUN_CACHE_DIR, then in-memory. A golden route
    # pins its cassette in the route directory and passes it here, because the autouse
    # fixture that clears LONGRUN_* would otherwise leave the env var with nothing in it and
    # every forecast key would miss.
    store = cache_path or cache_path_from_env()
    with SqliteCache(store, offline=offline) as cache:
        layers = FileLayerStore(root)
        # Scope 6.4: every source in the manifest carries a vintage. The store is what the
        # scorers ask, so the pins go in here rather than being stitched on afterwards - a
        # coverage entry then reports the vintage of the data it actually read.
        for layer, vintage in snapshot.layer_vintages.items():
            layers.set_vintage(layer, vintage)

        # Off unless asked. A local fixture of the same name still wins, so this only fills
        # gaps - and a GDAL /vsicurl/ read bypasses the cache, the budget and
        # LONGRUN_OFFLINE, which is a hole worth keeping deliberate (ADR 0007).
        remote = remote_raster_map(route, remote_rasters)
        budget = Budget()
        # Imported here rather than at module scope so `longrun --version` does not pay for
        # entry-point scanning, and so a third-party adapter that will not import cannot
        # break a command that never asked for one. `discover()` reports rather than raises,
        # but the import itself is still work nobody asked for on most invocations.
        from longrun.adapters.registry import AdapterRegistry

        yield ScorerContext(
            layers=layers,
            rasters=FileRasterStore(root, remote=remote, offline=offline, budget=budget),
            cache=cache,
            clock=FrozenClock(start_at),
            coverage=CoverageManifest(),
            profile=profile,
            budget=budget,
            snapshot=snapshot.layer_vintages,
            features=AdapterRegistry(cache, budget, offline=offline),
            utc_offset_hours=utc_offset,
        )


def remote_raster_map(route: Any, enabled: bool) -> dict[str, str]:
    """National raster URLs, or nothing at all when remote reads were not asked for."""
    if not enabled:
        return {}
    from longrun.core.data.rasters import three_dep_url
    from longrun.core.geo.projections import centroid

    middle = centroid(route)
    return {"dem": three_dep_url(middle.lat, middle.lon)}


__all__ = ["open_context", "remote_raster_map"]
