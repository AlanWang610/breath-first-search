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
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

#: Set to 1 to make a cache miss an error. Golden and contract tests run this way.
OFFLINE_ENV_VAR = "LONGRUN_OFFLINE"

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


class SqliteCache:
    """A `Cache` backed by SQLite. Satisfies `core.data.base.Cache`."""

    def __init__(self, path: Path | str = ":memory:", offline: bool = False) -> None:
        self._path = path
        self._offline = offline
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
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
        """Return a cached value, calling `producer()` only on a miss.

        The single path every external call should take, so that the offline guarantee
        holds without each adapter remembering to check.
        """
        key = args_hash(args)
        day_str = day.isoformat() if isinstance(day, date) else day
        hit = self.get(tool, key, day_str)
        if hit is not None:
            return hit
        value = producer()
        self.put(tool, key, day_str, value)
        return value

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
