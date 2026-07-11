"""MCP tools for the compose capability group."""

from app.core import (
    READ_ONLY_ANNOTATIONS,
    authorize_tool,
    mcp,
    require_scope,
    shell,
    shlex,
)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def compose_status(cwd: str = ".") -> str:
    """Return Docker Compose service state for a project."""
    authorize_tool("compose_status")
    require_scope("deploy:run")
    return shell("docker compose ps --all", cwd)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def compose_logs(service: str | None = None, tail: int = 200, cwd: str = ".") -> str:
    """Return bounded recent Docker Compose logs."""
    authorize_tool("compose_logs")
    require_scope("deploy:run")
    suffix = f" {shlex.quote(service)}" if service else ""
    return shell(f"docker compose logs --tail {min(max(tail, 1), 2000)} --no-color{suffix}", cwd)


@mcp.tool()
def compose_restart(services: list[str] | None = None, cwd: str = ".") -> str:
    """Restart all or selected Docker Compose services."""
    authorize_tool("compose_restart")
    require_scope("deploy:run")
    require_scope("workspace:write")
    suffix = " ".join(shlex.quote(service) for service in services or [])
    return shell(f"docker compose restart {suffix}".rstrip(), cwd)


TOOL_EXPORTS = ["compose_status", "compose_logs", "compose_restart"]
