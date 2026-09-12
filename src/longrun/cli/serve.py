"""`longrun serve` - the MCP server over stdio.

Scope 10.2 puts chat at the surface where elicitation, same-tier trade-offs and preference
updates belong. This is the door: a local chat client speaks stdio, and every tool it finds
is one the CLI could already run, which is scope 3.9's rule in the direction it asks for.
"""

from __future__ import annotations

from pathlib import Path

import typer


def serve(
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    cache_path: Path | None = typer.Option(None, "--cache", help="Cache/cassette file."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    router_url: str | None = typer.Option(None, "--router", help="GraphHopper base URL."),
) -> None:
    """Serve the scope 7 tools over MCP stdio."""
    from longrun.core.data.cache import offline_from_env
    from longrun.tools.base import ToolSettings
    from longrun.tools.server import serve as run_server

    settings = ToolSettings.from_env()
    settings = ToolSettings(
        root=fixtures or settings.root,
        cache_path=cache_path or settings.cache_path,
        offline=offline or offline_from_env(),
        snapshot=settings.snapshot,
        router_url=router_url or settings.router_url,
    )
    run_server(settings)


def register(app: typer.Typer) -> None:
    app.command("serve")(serve)


__all__ = ["register", "serve"]
