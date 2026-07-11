"""MCP tools for the snapshots capability group."""

from app.core import (
    authorize_tool,
    mcp,
    require_scope,
    user_snapshot_root,
)


@mcp.tool()
def list_snapshots() -> str:
    authorize_tool("list_snapshots")
    require_scope("workspace:read")
    return "\n".join(sorted(path.name for path in user_snapshot_root().iterdir() if path.is_dir()))


TOOL_EXPORTS = ["list_snapshots"]
