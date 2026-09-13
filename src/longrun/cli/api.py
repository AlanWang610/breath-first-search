"""`longrun api` - the HTTP surface the web UI runs against (scope 10.3).

The third door onto the same `tools/` layer, after the CLI itself and the MCP server. It
starts no capability of its own: every endpoint calls something `longrun` could already do
from a shell, which is scope 3.9's rule and the reason this milestone is last.

Binds to localhost by default and says so. This serves a local planner against a local
GraphHopper and a local profile; a `--host 0.0.0.0` is a deliberate act and not a default
somebody inherits.
"""

from __future__ import annotations

from pathlib import Path

import typer


def api(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Localhost by default."),
    port: int = typer.Option(8000, "--port", help="Bind port."),
    plans: Path = typer.Option(Path("plans"), "--plans", help="Where stored plans live."),
    ui: Path | None = typer.Option(
        None, "--ui", help="Built UI directory to serve at /, e.g. ui/dist."
    ),
    reload: bool = typer.Option(False, "--reload", help="Reload on source changes."),
) -> None:
    """Serve the scope 10.3 API over HTTP."""
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
        typer.echo(
            "error: the api extra is not installed; `uv sync --extra api`",
            err=True,
        )
        raise typer.Exit(code=2) from exc

    from longrun.api.app import create_app

    if ui is not None and not ui.is_dir():
        # A missing UI directory is worth saying out loud rather than quietly serving an
        # API with no pages: the likeliest cause is that `npm run build` has not been run,
        # and a 404 at `/` is a confusing way to learn that.
        typer.echo(f"warning: {ui} does not exist; serving the API without a UI", err=True)
        ui = None

    typer.echo(f"serving on http://{host}:{port} (plans in {plans})")
    uvicorn.run(create_app(plans_dir=plans, ui_dir=ui), host=host, port=port, reload=reload)


def register(app: typer.Typer) -> None:
    app.command("api")(api)


__all__ = ["api", "register"]
