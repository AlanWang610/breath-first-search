"""External-result cache, keyed by (tool, args hash, date) (scope 4.4).

This module does double duty, deliberately. It is the production cache that keeps a plan
inside its external-call budget (scope 6.4), and it is the **test cassette store**: a
pre-populated cache opened in offline mode is a recorded fixture, and a miss becomes a
loud, specific failure instead of a silent network call. That is why there is no separate
recording library anywhere in this project.

The date is part of the key because almost everything cached here is a forecast or a
closure list, which are only meaningful for the day they describe. It also means a golden
route's date is frozen forever: a forecast for a past date can never be re-fetched, so
fixtures pin an explicit date and never a relative one.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from longrun.core.data.base import Cache
    from longrun.core.models.plan import ToolCall  # pragma: no cover

#: Set to 1 to make a cache miss an error. Golden and contract tests run this way.
OFFLINE_ENV_VAR = "LONGRUN_OFFLINE"

#: Where to keep the cache between runs. Unset means in-memory, so nothing is written
#: outside a configured directory and a test run leaves no trace.
CACHE_DIR_ENV_VAR = "LONGRUN_CACHE_DIR"

#: Accepted spellings of "yes". A bare `LONGRUN_OFFLINE=0` must not read as truthy, which
#: a plain truthiness check on the string would get wrong.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: ~11 m. Finer than this and a cache key depends on floating-point noise: a point
#: recomputed rather than re-read misses a cassette it is identical to. `args_hash`
#: deliberately does no rounding of its own, so every key builder applies this.
COORD_PRECISION = 4

#: For a lookup whose answer does not vary with the date: a coordinate's forecast grid,
#: a geocode, a route. Keying those by the plan date would re-fetch them daily for
#: nothing and multiply cassette size. A deliberate, named abuse of the `day` column
#: rather than an accident - and what a route depends on instead goes in the args,
#: because a route is a function of the graph it was drawn on.
STATIC_DAY = "static"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    tool      TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    day       TEXT NOT NULL,
    value     TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY (tool, args_hash, day)
);
"""


class CacheMiss(LookupError):
    """An offline cache was asked for something it does not hold.

    Carries the key so the failure names what was missing and, for a test, exactly which
    cassette needs recording.
    """

    def __init__(self, tool: str, args_hash: str, day: str) -> None:
        super().__init__(
            f"offline cache miss: {tool}(args={args_hash[:12]}) for {day}. "
            f"Record this cassette, or mark the test `network`."
        )
        self.tool = tool
        self.args_hash = args_hash
        self.day = day


def args_hash(args: Any) -> str:
    """A stable hash of a tool's arguments.

    Stable across processes and platforms, which Python's `hash()` is not: it is salted
    per process, so using it would make cache keys — and therefore recorded cassettes —
    non-reproducible.
    """
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def offline_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Whether `LONGRUN_OFFLINE` asks for no-miss mode.

    The environment variable and the CLI's `--offline` flag are two doors to one setting,
    and either one alone is enough to turn it on: a caller that has gone to the trouble of
    exporting it should not have that silently ignored.
    """
    source = os.environ if env is None else env
    return source.get(OFFLINE_ENV_VAR, "").strip().lower() in _TRUTHY


def cache_path_from_env(env: Mapping[str, str] | None = None) -> Path | str:
    """The configured on-disk cache file, or `:memory:` when none is configured.

    In-memory is the honest default rather than a guess at a good location: a cache that
    silently materialises files under the current working directory is a surprise, and it
    would put a database inside the repository every time the test suite runs the CLI.
    """
    source = os.environ if env is None else env
    directory = source.get(CACHE_DIR_ENV_VAR, "").strip()
    if not directory:
        return ":memory:"
    return Path(directory).expanduser() / "cache.sqlite"


class SqliteCache:
    """A `Cache` backed by SQLite. Satisfies `core.data.base.Cache`."""

    def __init__(self, path: Path | str = ":memory:", offline: bool = False) -> None:
        self._path = path
        self._offline = offline
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        #: Scope 6.4's tool-call record. Kept on the cache rather than passed to every
        #: call site because the cache is already the one door and already per-plan.
        self.calls: list[ToolCall] = []
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def offline(self) -> bool:
        return self._offline

    def get(self, tool: str, key: str, day: str) -> Any | None:
        """Return a cached value, or None on a miss.

        Offline mode raises `CacheMiss` instead of returning None, because a caller that
        treats a miss as "no data" would silently produce a plan with an unreported gap.
        """
        row = self._conn.execute(
            "SELECT value FROM cache WHERE tool=? AND args_hash=? AND day=?",
            (tool, key, day),
        ).fetchone()
        if row is None:
            if self._offline:
                raise CacheMiss(tool, key, day)
            return None
        return json.loads(row[0])

    def put(self, tool: str, key: str, day: str, value: Any) -> None:
        """Store a value. A no-op in offline mode, so a cassette is never mutated."""
        if self._offline:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (tool, args_hash, day, value, stored_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (tool, key, day, json.dumps(value, default=str)),
        )
        self._conn.commit()

    def fetch(
        self,
        tool: str,
        args: Any,
        day: date | str,
        producer: Any,
    ) -> Any:
        """Convenience wrapper over the module-level `fetch`; see its docstring."""
        return fetch(self, tool, args, day, producer)

    def keys(self) -> list[tuple[str, str, str]]:
        """Every key held, for inspecting a cassette."""
        return [
            (t, h, d)
            for t, h, d in self._conn.execute(
                "SELECT tool, args_hash, day FROM cache ORDER BY tool, day, args_hash"
            )
        ]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def fetch(cache: Cache, tool: str, args: Any, day: date | str, producer: Callable[[], Any]) -> Any:
    """Return a cached value, calling `producer()` only on a miss.

    **The single path every external call takes.** A free function rather than a method on
    the `Cache` protocol, because this is where the offline guarantee lives: a second
    implementation that had to write its own `fetch` could forget to let `CacheMiss` escape,
    and the failure would be a golden test quietly reaching the network instead of failing.
    One implementation, one guarantee, and any two-method `Cache` gets it.

    A producer returning `None` is an error rather than a cached nothing. SQLite stores the
    value as JSON, so `None` round-trips to a value indistinguishable from a miss and the
    producer would be re-called on every subsequent request — spending budget and, offline,
    raising `CacheMiss` for a key that was recorded. Callers that mean "no data here" return
    a structured empty payload, which is also what makes the absence reportable.
    """
    key = args_hash(args)
    day_str = day.isoformat() if isinstance(day, date) else str(day)
    started = time.perf_counter()
    hit = cache.get(tool, key, day_str)
    if hit is not None:
        _record(cache, tool, key, started, cached=True)
        return hit
    value = producer()
    if value is None:
        raise ValueError(
            f"{tool} producer returned None; return a structured empty payload instead, "
            "or None will read as a cache miss on every later call"
        )
    cache.put(tool, key, day_str, value)
    _record(cache, tool, key, started, cached=False)
    return value


def _record(cache: Cache, tool: str, key: str, started: float, *, cached: bool) -> None:
    """Log one trip through the door, hit or miss.

    A miss that *raised* - an offline `CacheMiss`, a dead endpoint, a budget refusal - is
    deliberately not logged: it did not happen, and the reason it did not is already
    reported through the coverage manifest, which is where scope 3.6 puts it.
    """
    from longrun.core.models.plan import ToolCall

    log = getattr(cache, "calls", None)
    if log is None:  # pragma: no cover - every cache in this codebase keeps one
        return
    log.append(
        ToolCall(
            tool=tool,
            elapsed_s=time.perf_counter() - started,
            args_hash=key,
            cached=cached,
        )
    )
