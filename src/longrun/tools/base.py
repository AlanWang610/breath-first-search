"""What every tool needs: where the data is, and how to open a context around a route.

Scope 4.1 calls these "stateless, Pydantic-schema'd tools". Stateless means per call: a
tool is handed a route and a date and opens its own context, so two calls share nothing
and the server holds no plan between them. What it does *not* mean is configuration-free -
where the fixtures are and which cassette to replay are properties of the process, not of
the question, and passing them on every call would put them in every schema.

Every tool returns a mapping rather than raising. A missing layer, an absent adapter or a
cassette with no answer is a *reason* in the result, which is scope 3.6's rule applied to
the one surface where a caller is a program rather than a person - and a program reading a
stack trace learns less than one reading `{"checked": false, "reason": ...}`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import TYPE_CHECKING, Any

from longrun.core.models.plan import SnapshotPins

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route

#: Where a tool server looks for layers and rasters when nothing says otherwise.
FIXTURES_ENV_VAR = "LONGRUN_FIXTURES"


@dataclass(frozen=True)
class ToolSettings:
    """One per server process, read at start-up from the environment."""

    root: Path = Path("data")
    cache_path: Path | None = None
    offline: bool = False
    snapshot: SnapshotPins = field(default_factory=SnapshotPins)
    router_url: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ToolSettings:
        """Read at call time, never at import - `conftest._clear_longrun_env`'s rule."""
        from longrun.core.data.cache import cache_path_from_env, offline_from_env

        source = os.environ if env is None else env
        root = source.get(FIXTURES_ENV_VAR, "").strip() or source.get("LONGRUN_DATA_DIR", "")
        cache = cache_path_from_env(source)
        return cls(
            root=Path(root) if root else Path("data"),
            cache_path=Path(cache) if isinstance(cache, Path) else None,
            offline=offline_from_env(source),
            router_url=source.get("LONGRUN_GRAPHHOPPER_URL", "").strip() or None,
        )


def read_route(gpx_path: str) -> Route:
    """`import_route` (scope 7.8), which is `gpx_read` under the name the scope gives it."""
    from longrun.core.geo.gpx import gpx_read

    path = Path(gpx_path)
    return gpx_read(path, route_id=path.stem)


def start_of(date: str, start: str = "07:00") -> datetime:
    hour, _, minute = start.partition(":")
    return datetime.combine(
        datetime.strptime(date, "%Y-%m-%d").date(), time_type(int(hour), int(minute or 0))
    )


@contextmanager
def scoring_context(
    settings: ToolSettings, route: Route, start_at: datetime
) -> Iterator[ScorerContext]:
    """The same context `repair` and the loop score against, opened for one call."""
    from longrun.runtime import open_context

    with open_context(
        route=route,
        root=settings.root,
        snapshot=settings.snapshot,
        start_at=start_at,
        profile=_profile(),
        offline=settings.offline,
        cache_path=settings.cache_path,
    ) as ctx:
        yield ctx


def _profile() -> Any:
    from longrun.core.preferences.store import load_profile

    return load_profile()


def score_with(settings: ToolSettings, scorer: str, gpx_path: str, date: str, start: str) -> Any:
    """Run one scorer over one route and return its result as a mapping.

    The scorer list lives in `core/scorers/registry.py`, which M5.1 moved out of `cli/` for
    exactly this: the tool layer and the agent loop need the same list and may not import
    a CLI command.
    """
    from longrun.core.plan.pipeline import score_once
    from longrun.core.scorers.registry import SCORERS, load_scorer

    module_path = SCORERS.get(scorer)
    if module_path is None:
        return unavailable(scorer, f"no scorer named {scorer!r}")
    if load_scorer(module_path) is None:  # pragma: no cover - every listed scorer imports
        return unavailable(scorer, "scorer not implemented yet")

    route = read_route(gpx_path)
    start_at = start_of(date, start)
    with scoring_context(settings, route, start_at) as ctx:
        scored = score_once(route, _request(route, start_at), ctx, start_at=start_at)
    result = next((r for r in scored.results if r.name == scorer), None)
    if result is None:  # pragma: no cover
        return unavailable(scorer, "the pass produced no result for this scorer")
    return result.model_dump(mode="json")


def _request(route: Route, start_at: datetime) -> Any:
    from longrun.core.models.request import PlanRequest

    return PlanRequest(mode="repair", date=start_at.date(), start_time=start_at.time())


def register_scorers(server: Any, settings: ToolSettings, mapping: dict[str, str]) -> None:
    """Register one tool per scorer, so a group module is a declaration.

    Every scope 7.2/7.4/7.5/7.6/7.7 tool has the same shape - a route, a date, a start
    time, a `ScorerResult` back - so writing eight near-identical closures by hand would
    be eight places for them to drift.
    """
    for tool_name, scorer in mapping.items():
        _register_one(server, settings, tool_name, scorer)


def _register_one(server: Any, settings: ToolSettings, tool_name: str, scorer: str) -> None:
    @server.tool(name=tool_name, description=f"Scope 7: {scorer} over a GPX route.")
    def run(gpx_path: str, date: str, start: str = "07:00") -> dict[str, Any]:
        return dict(score_with(settings, scorer, gpx_path, date, start))


def unavailable(name: str, reason: str, *, milestone: str | None = None) -> dict[str, Any]:
    """A tool that cannot answer says so in the shape a tool that can would have used.

    The `NOT_YET_IMPLEMENTED` treatment M4 proved: a capability the scope names and this
    build does not have is reported by name, with the reason, rather than omitted from the
    tool list - because a caller cannot tell an absent tool from a tool that found nothing.
    """
    out: dict[str, Any] = {"name": name, "checked": False, "reason": reason}
    if milestone:
        out["milestone"] = milestone
    return out


__all__ = [
    "FIXTURES_ENV_VAR",
    "ToolSettings",
    "read_route",
    "register_scorers",
    "score_with",
    "scoring_context",
    "start_of",
    "unavailable",
]
