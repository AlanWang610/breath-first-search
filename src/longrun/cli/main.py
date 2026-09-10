"""Typer application root (scope 10.1).

This is the test harness as much as it is the user interface: every golden test runs
through these commands, with no model in the loop. Commands are registered here as their
layers land; the module exists from the first commit so `[project.scripts]` resolves.
"""

from __future__ import annotations

import typer

from longrun import __version__
from longrun.cli import export, freeze, plan, region, repair

app = typer.Typer(
    name="longrun",
    help="Verified routes and plan sheets for 20-100 km runs.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"longrun {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Long-run planner."""


repair.register(app)
freeze.register(app)
export.register(app)
region.register(app)
plan.register(app)


if __name__ == "__main__":  # pragma: no cover
    app()
