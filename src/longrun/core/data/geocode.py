"""A place name becomes a coordinate (scope 8.1 step 1).

`cli/plan.py` has said since M3 that it "does not geocode" and that "the agent is where a
place name becomes a coordinate". This is that, and it is the last thing standing between
a sentence and a `PlanRequest`.

Nominatim, because the scope names no geocoder at all and this one is keyless, serves the
OSM data the rest of the project already runs on, and needs no account (ADR 0018). Its
usage policy asks for a contact string, and that string is **never invented or borrowed** -
`LONGRUN_NOMINATIM_USER_AGENT` is set by the user, exactly as `LONGRUN_NWS_USER_AGENT` is,
and without it this makes no call at all and says which variable is missing.

Built to `air.py`'s shape: a named args builder that rounds, a bare transport function, a
producer that spends the budget as its first statement so a replay is free, and a
dedicated `BudgetExceeded` before the general handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from longrun.core.data.cache import STATIC_DAY, CacheMiss, fetch
from longrun.core.models.context import BudgetExceeded
from longrun.core.models.coverage import CoverageEntry

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import LatLon

NOMINATIM_ROOT = "https://nominatim.openstreetmap.org/search"
USER_AGENT_ENV_VAR = "LONGRUN_NOMINATIM_USER_AGENT"
HTTP_TIMEOUT_S = 20.0

#: The source name every coverage entry uses, answered *and* unanswered. It has to resolve
#: through `licence_for`, because `test_every_recorded_source_has_a_licence` fails a golden
#: on `LICENCE NOT RECORDED` - and an unanswered entry with a different name is exactly how
#: `forecast.py` acquired that bug.
SOURCE = "nominatim"

#: Nominatim's policy caps automated use at one request a second. A plan geocodes its
#: endpoints once and then replays them from the cache, so this is a ceiling nothing
#: approaches - recorded because the next person to loop over a list should know it exists.
MAX_LOOKUPS_PER_PLAN = 8


@dataclass(frozen=True)
class Place:
    """One candidate for a name."""

    name: str
    lat: float
    lon: float
    kind: str | None = None


@dataclass(frozen=True)
class Geocoded:
    """What a lookup produced, and why it produced nothing."""

    query: str
    places: list[Place] = field(default_factory=list)
    reason: str | None = None

    @property
    def answered(self) -> bool:
        return bool(self.places)

    @property
    def best(self) -> Place | None:
        return self.places[0] if self.places else None

    def coverage(self) -> list[CoverageEntry]:
        if self.answered:
            return [
                CoverageEntry(
                    source=SOURCE,
                    kind="geocode",
                    checked=True,
                    reason=f"{len(self.places)} match(es) for {self.query!r}",
                    confidence=1.0 if len(self.places) == 1 else 0.6,
                )
            ]
        return [
            CoverageEntry(
                source=SOURCE,
                kind="geocode",
                checked=False,
                reason=self.reason or f"no match for {self.query!r}",
            )
        ]


def user_agent(env: dict[str, str] | None = None) -> str | None:
    """The contact string, or None when nobody set one. Read at call time."""
    import os

    source = os.environ if env is None else env
    return source.get(USER_AGENT_ENV_VAR, "").strip() or None


def geocode_args(query: str, *, limit: int = 5, viewbox: tuple[float, ...] | None = None) -> Any:
    """Cache-key inputs. Named so a stability test can pin the hash."""
    args: dict[str, Any] = {"q": query.strip().lower(), "limit": limit, "format": "jsonv2"}
    if viewbox is not None:
        # Rounded for the reason every other key builder rounds: `args_hash` does not, and
        # a box recomputed from a route's bbox would otherwise miss its own recording.
        args["viewbox"] = [round(v, 4) for v in viewbox]
        args["bounded"] = 1
    return args


def parse_places(payload: Any) -> list[Place]:
    """Nominatim's `jsonv2` array, with anything unreadable dropped rather than guessed."""
    out: list[Place] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            # A row that is not a mapping at all - the handler below catches a *bad field*
            # and `row.get` on an int raises past it. An endpoint that returns something
            # unexpected must not take a plan down three layers away.
            continue
        try:
            out.append(
                Place(
                    name=str(row.get("display_name") or row.get("name") or "?"),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    kind=row.get("type"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def geocode(query: str, ctx: ScorerContext, *, limit: int = 5) -> Geocoded:
    """Look a place name up, through the cache like every other external source."""
    contact = user_agent()
    if contact is None:
        return Geocoded(
            query=query,
            reason=(
                f"{USER_AGENT_ENV_VAR} is not set; Nominatim's usage policy requires a "
                "contact string (https://operations.osmfoundation.org/policies/nominatim/)"
            ),
        )

    args = geocode_args(query, limit=limit)

    def producer(args: Any = args) -> Any:
        ctx.budget.spend_api_call()
        return _get(args, contact)

    try:
        payload = fetch(ctx.cache, "nominatim.search", args, STATIC_DAY, producer)
    except BudgetExceeded:
        return Geocoded(query=query, reason="external API budget exhausted for this plan")
    except Exception as exc:  # noqa: BLE001 - a lookup that failed is a reason, not a crash
        reason = "not in the cassette" if isinstance(exc, CacheMiss) else f"{type(exc).__name__}"
        return Geocoded(query=query, reason=reason)

    places = parse_places(payload)
    return Geocoded(
        query=query,
        places=places,
        reason=None if places else f"no match for {query!r}",
    )


def to_latlon(place: Place) -> LatLon:
    from longrun.core.models.geometry import LatLon

    return LatLon(lat=place.lat, lon=place.lon)


def _get(args: Any, contact: str) -> Any:
    """The transport, and nothing else. `httpx` imported here, not at module scope."""
    import httpx

    response = httpx.get(
        NOMINATIM_ROOT,
        params=args,
        headers={"User-Agent": contact, "Accept": "application/json"},
        timeout=HTTP_TIMEOUT_S,
        follow_redirects=True,
    )
    response.raise_for_status()
    # Never `None`: `fetch` raises on it, because a `None` round-trips as a miss forever.
    return response.json() or []


__all__ = [
    "HTTP_TIMEOUT_S",
    "MAX_LOOKUPS_PER_PLAN",
    "NOMINATIM_ROOT",
    "SOURCE",
    "USER_AGENT_ENV_VAR",
    "Geocoded",
    "Place",
    "geocode",
    "geocode_args",
    "parse_places",
    "to_latlon",
    "user_agent",
]
