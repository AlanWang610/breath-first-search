"""Shared fixtures.

Golden and contract tests must not touch the network: adapters are served from recorded
cassettes, and the router is either a local GraphHopper instance or a recorded response.
Anything that genuinely needs a live service is marked `network` and skipped by default.

That claim is enforced here rather than merely stated: `_block_network` fails any test not
marked `network` that opens an off-host socket, so "a test reached the internet" is a loud,
specific failure instead of a slow, flaky one.

`_clear_longrun_env` does the same job for configuration: the suite must not pass or fail
because of what someone happens to have exported.
"""

from __future__ import annotations

import os
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


@pytest.fixture(autouse=True)
def _block_model(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail non-`network` tests that reach a language model (ADR 0015).

    The socket block above would catch it too, eventually and obscurely. This makes it a
    loud, specific failure at the layer that caused it - `RuntimeError: Model requests are
    not allowed` - which is what `_block_network` does for sockets and for the same reason.

    It also matters that the *loop* never needs one: every call site has a deterministic
    path, so a suite that silently acquired a model dependency would still pass on a
    developer's machine and fail in CI. This makes it fail on both.
    """
    if request.node.get_closest_marker("network"):
        yield
        return
    try:
        from pydantic_ai import models
    except ImportError:  # pragma: no cover - the `agent` extra is not installed
        yield
        return

    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    try:
        yield
    finally:
        models.ALLOW_MODEL_REQUESTS = previous


@pytest.fixture(autouse=True)
def _clear_longrun_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Run every test against a clean `LONGRUN_*` environment.

    Ambient configuration is exactly the kind of thing that makes a suite pass on one
    machine and fail on another: a developer with `LONGRUN_OFFLINE=1` exported would see
    different behaviour from CI, and the difference would look like a flaky test rather
    than a stale shell. A test that wants one of these sets it explicitly.
    """
    for name in [key for key in os.environ if key.startswith("LONGRUN_")]:
        monkeypatch.delenv(name, raising=False)
    # `ANTHROPIC_API_KEY` does not start with `LONGRUN_`, so the loop above never saw it -
    # and a developer with a real key exported would have had tests reaching a model that
    # pass in CI, which is the exact difference this fixture exists to prevent.
    #
    # Kept for `network` tests, which are the ones allowed to use it: a live test that
    # cleared the credential it needs would skip forever and look like it was passing.
    if not request.node.get_closest_marker("network"):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--update-golden` rewrites golden expectations instead of asserting against them.

    Declared here because pytest only reads `pytest_addoption` from the root conftest.
    """
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite tests/golden/routes/*/expected.json from this run, then review the diff.",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))
