"""MCP tools for inspecting and cleaning tracked runtime resources."""

from app.core import (
    BROWSER_IDLE_TTL_SECONDS,
    FINISHED_PROCESS_RETENTION_SECONDS,
    MAX_BROWSER_SESSIONS_PER_USER,
    MAX_PROCESSES_PER_USER,
    PROCESS_IDLE_TTL_SECONDS,
    RESOURCE_CLEANUP_INTERVAL_SECONDS,
    _cleanup_state_resources,
    _format_browser_result,
    _process_status,
    authorize_tool,
    mcp,
    session_state,
    time,
)


@mcp.tool()
def resource_status() -> str:
    """Return tracked processes, browser sessions, idle ages, and configured lifecycle limits."""
    authorize_tool("resource_status")
    state = session_state()
    now = time.time()
    processes = []
    for process_id, record in state.processes.items():
        processes.append(
            {
                "process_id": process_id,
                "pid": record.process.pid,
                "status": _process_status(record),
                "created_at_unix": record.started_at,
                "last_activity_at_unix": record.last_activity_at,
                "idle_seconds": round(max(now - record.last_activity_at, 0), 1),
                "exit_time_unix": record.exited_at,
                "output_lines": record.total_lines,
                "retained_output_characters": sum(len(line) for line in record.output),
            }
        )

    browser_sessions = []
    for session_id, record in state.browser_sessions.items():
        browser_sessions.append(
            {
                "session_id": session_id,
                "url": record.page.url,
                "created_at_unix": record.created_at,
                "last_activity_at_unix": record.last_activity_at,
                "idle_seconds": round(max(now - record.last_activity_at, 0), 1),
            }
        )

    return _format_browser_result(
        {
            "subject": state.subject,
            "limits": {
                "max_processes_per_user": MAX_PROCESSES_PER_USER,
                "max_browser_sessions_per_user": MAX_BROWSER_SESSIONS_PER_USER,
                "process_idle_ttl_seconds": PROCESS_IDLE_TTL_SECONDS,
                "browser_idle_ttl_seconds": BROWSER_IDLE_TTL_SECONDS,
                "finished_process_retention_seconds": FINISHED_PROCESS_RETENTION_SECONDS,
                "cleanup_interval_seconds": RESOURCE_CLEANUP_INTERVAL_SECONDS,
                "zero_disables_limit_or_expiration": True,
            },
            "counts": {
                "processes": len(processes),
                "browser_sessions": len(browser_sessions),
            },
            "processes": processes,
            "browser_sessions": browser_sessions,
        }
    )


@mcp.tool()
async def resource_cleanup(force: bool = False) -> str:
    """Clean eligible stale resources for the current identity. Set force=true to close all tracked resources."""
    authorize_tool("resource_cleanup")
    result = await _cleanup_state_resources(session_state(), force=force)
    result["force"] = force
    return _format_browser_result(result)


TOOL_EXPORTS = ["resource_status", "resource_cleanup"]
