"""Architectural invariants, enforced statically.

The README states these as ground rules and the scope as design principles (4.1, 3.3).
A rule an agent can forget to follow while writing a scorer is a rule that needs a test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "longrun"

# scope 4.1: core/ is the deterministic library. Nothing above it may be imported from it.
#
# `longrun.jobs` joined the list in M5.5. The tuple is hand-maintained and was written
# before `jobs/` was a layer anyone could import, so `core/` importing it would have passed
# this test - and M5.6 is the milestone that makes the import possible.
FORBIDDEN_FROM_CORE = (
    "longrun.agent",
    "longrun.tools",
    "longrun.cli",
    "longrun.api",
    "longrun.jobs",
)


def _python_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", _python_files("core"), ids=lambda p: p.name)
def test_core_does_not_import_outer_layers(path: Path) -> None:
    """core/ has zero dependency on the agent, tools, CLI or API layers (scope 4.1)."""
    imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8"), str(path)))
    offenders = sorted(
        name
        for name in imported
        for forbidden in FORBIDDEN_FROM_CORE
        if name == forbidden or name.startswith(forbidden + ".")
    )
    assert not offenders, f"{path.relative_to(SRC)} imports outer layers: {offenders}"


@pytest.mark.parametrize("path", _python_files("core"), ids=lambda p: p.name)
def test_core_has_no_implicit_clock(path: Path) -> None:
    """Time is always a parameter in core/ (scope 3.3).

    `datetime.now()` and `date.today()` make every time-dependent scorer untestable and
    every golden route non-reproducible. The clock arrives on ScorerContext instead.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    offenders = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "utcnow", "today"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"datetime", "date", "time"}
    ]
    assert not offenders, f"{path.relative_to(SRC)} reads an implicit clock: {offenders}"
