"""The MCP server: one layer serving the CLI, a chat client, the loop and the web UI.

Scope 3.9 is the reason this exists before any UI does - "any capability exists as a tool
and a CLI path before it gets a UI control" - and scope 4.1 is the reason it is thin: the
tools wrap functions that already exist, and a wrapper that did anything else would be a
second implementation for the golden suite not to watch.

**Sync tools, deliberately.** `mcp` 2.x runs a synchronous tool through
`anyio.to_thread.run_sync`, so `core/` stays synchronous and the pool does the threading
(ADR 0016). This also avoids a failure that only appears with a real model in place: a
tier-4 extraction calls pydantic-ai's `run_sync`, which raises inside a running event
loop - so a tool that reached a scorer *on* the loop thread would work in every test and
break in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.tools import (
    access,
    editing,
    environment,
    io,
    logistics,
    resupply,
    routing,
    runnability,
)
from longrun.tools.base import ToolSettings

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

#: Scope 7's groups, in the order the scope lists them.
GROUPS = (routing, runnability, environment, resupply, access, logistics, editing, io)

SERVER_NAME = "longrun"


def build_server(settings: ToolSettings | None = None, modules: Sequence[Any] | None = None) -> Any:
    """Assemble the server without running it, so a test can list its tools.

    Imported lazily: `mcp` is the `tools` extra, and `longrun --version` must not pay for
    it - the same argument `repair` makes about the adapter registry.
    """
    from mcp.server.mcpserver import MCPServer

    from longrun import __version__

    settings = settings or ToolSettings.from_env()
    server = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=(
            "Route planning for 20-100 km runs. Tools measure; they do not decide. "
            "A tool that cannot answer returns `checked: false` and a reason rather "
            "than failing - an absent data source is not an empty result."
        ),
    )
    for module in modules or GROUPS:
        module.register(server, settings)
    return server


def serve(settings: ToolSettings | None = None) -> None:  # pragma: no cover - a blocking loop
    """Run over stdio, which is what a local chat client speaks."""
    build_server(settings).run("stdio")


__all__ = ["GROUPS", "SERVER_NAME", "build_server", "serve"]
