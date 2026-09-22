"""One pipeline run per golden route, shared by every test that reads it.

Measured on 2026-09-21, `pytest -m "not network and not slow" --durations=40`: the whole
hermetic suite was **306 s and the golden tier was 272 s of it — 89%**. Not fixture size,
which is what ADR 0031 assumed when it raised the cap: four separate tests each called
`harness.run()` for every route, so six routes cost **24 full CLI pipeline runs where six
would do**. The other three assertions are read-only properties of the same `GoldenRun`.

`test_the_route_matches_its_expectation` already carried a comment worrying that "each new
one is another six full pipeline runs" and folding an assertion in rather than adding a
test — the per-test cost was understood, and that the four existing tests already paid it
four times over was not.

Nothing is weakened by sharing. `GoldenRun` is frozen, holding a `Plan` and three strings,
and no test mutates it. The one thing four independent runs bought incidentally was a
determinism check nobody wrote down; that claim has an explicit owner in
`test_repair_is_deterministic_apart_from_the_plan_id`, which is where it belongs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from tests.golden import harness

#: Route name -> the run, or the exception it raised. Session-lived, and deliberately not a
#: parametrized fixture: the tests take `name` from `parametrize`, and reaching it through
#: `request.getfixturevalue` would make the call site less obvious than calling a function.
_RUNS: dict[str, harness.GoldenRun | BaseException] = {}


@pytest.fixture(scope="session")
def golden_run(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], harness.GoldenRun]:
    """Run a route through the CLI once per session and hand back what it wrote.

    A failure is cached as well as a success. A route that cannot run at all fails four
    tests either way, and re-running a broken pipeline three more times to say so turns
    every `--update-golden` cycle into four slow failures instead of one.
    """

    def run(name: str) -> harness.GoldenRun:
        if name not in _RUNS:
            out: Path = tmp_path_factory.mktemp(f"golden-{name}-")
            try:
                _RUNS[name] = harness.run(harness.ROUTES_DIR / name, out / "out")
            except BaseException as exc:  # noqa: BLE001 - re-raised below, never swallowed
                _RUNS[name] = exc
        cached = _RUNS[name]
        if isinstance(cached, BaseException):
            raise cached
        return cached

    return run
