"""MCP tools for the database capability group."""

from app.core import (
    MAX_OUTPUT,
    _format_browser_result,
    _redact_text,
    _secret_values,
    authorize_tool,
    mcp,
    require_scope,
    resolve_path,
    subprocess,
)


@mcp.tool()
def database_query(
    engine: str, query: str, database: str | None = None, secret_ref: str | None = None, timeout_seconds: int = 30
) -> str:
    """Run a SQLite query against a workspace database or a PostgreSQL query using a named secret connection URL. Use only for trusted development databases."""
    authorize_tool("database_query")
    require_scope("database:use")
    timeout = min(max(timeout_seconds, 1), 120)
    redactions: list[str] = []
    if engine == "sqlite":
        if not database:
            raise ValueError("SQLite requires a workspace database path")
        path = resolve_path(database)
        result = subprocess.run(["sqlite3", "-json", str(path), query], text=True, capture_output=True, timeout=timeout)
    elif engine == "postgres":
        if not secret_ref:
            raise ValueError("Postgres requires a secret_ref containing its connection URL")
        connection = _secret_values([secret_ref])[secret_ref]
        redactions.append(connection)
        result = subprocess.run(
            ["psql", connection, "--no-psqlrc", "--tuples-only", "--no-align", "-c", query],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    else:
        raise ValueError("engine must be sqlite or postgres")
    return _format_browser_result(
        {
            "engine": engine,
            "exit_code": result.returncode,
            "stdout": _redact_text(result.stdout, redactions)[:MAX_OUTPUT],
            "stderr": _redact_text(result.stderr, redactions)[:MAX_OUTPUT],
        }
    )


TOOL_EXPORTS = ["database_query"]
