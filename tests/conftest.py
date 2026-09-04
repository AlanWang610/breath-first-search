"""Shared fixtures.

Golden and contract tests must not touch the network: adapters are served from recorded
cassettes, and the router is either a local GraphHopper instance or a recorded response.
Anything that genuinely needs a live service is marked `network` and skipped by default.

That claim is enforced here rather than merely stated: `_block_network` fails any test not
marked `network` that opens an off-host socket, so "a test reached the internet" is a loud,
specific failure instead of a slow, flaky one.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


class NetworkAccessError(RuntimeError):
    """A test that is not marked `network` attempted an off-host connection."""


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail non-`network` tests that open an off-host socket."""
    if request.node.get_closest_marker("network"):
        yield
        return

    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if str(host) not in _ALLOWED_HOSTS:
            raise NetworkAccessError(
                f"blocked connection to {host!r}: mark the test `network`, or serve it "
                f"from the cache in offline mode"
            )
        return real_connect(self, address, *args, **kwargs)

    socket.socket.connect = guarded  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures"
