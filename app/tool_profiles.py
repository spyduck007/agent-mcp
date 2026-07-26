"""MCP tool exposure profiles.

All tool implementations remain imported and available through the Python facade.
Profiles only control which tools FastMCP publishes to clients such as ChatGPT.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

CHATGPT_TOOL_NAMES = frozenset(
    {
        # High-level browser automation and persistent sessions.
        "browser_inspect",
        "browser_interact",
        "browser_accessibility_snapshot",
        "browser_session_open",
        "browser_session_list",
        "browser_session_close",
        "browser_session_inspect",
        "browser_session_interact",
        "browser_session_evaluate",
        "browser_session_logs",
        "browser_session_upload",
        "browser_session_download",
        "browser_session_popup",
        "browser_session_trace",
        # Arbitrary foreground execution.
        "run_command",
        "run_command_advanced",
        # Compose, database, snapshots, and deployment.
        "compose_status",
        "compose_logs",
        "database_query",
        "create_snapshot",
        "restore_snapshot",
        "list_snapshots",
        "deployment_preflight",
        "deployment_apply",
        "deployment_rollback",
        # Focused file inspection and mutation. Shell remains the escape hatch.
        "read_file",
        "read_files",
        "atomic_write_file",
        "replace_in_file",
        "replace_lines",
        "copy_path",
        "move_path",
        "delete_path",
        "create_directory",
        "list_files",
        "tree",
        "search_all_matches",
        "find_symbol",
        "stat_path",
        # Core Git plus universal authenticated GitHub CLI.
        "apply_patch",
        "git_status",
        "git_diff",
        "git_log",
        "git_checkout",
        "git_commit",
        "git_restore",
        "git_pull",
        "git_push",
        "git_worktree",
        "github_cli",
        # HTTP primitives.
        "http_request",
        "http_download",
        "http_upload",
        # Unified package management.
        "package_install",
        "package_remove",
        "install_project_dependencies",
        # Background process control.
        "start_process_advanced",
        "list_processes",
        "get_process_output",
        "wait_for_process_output",
        "stop_process",
        "send_process_input",
        "signal_process",
        # Grounded project context and durable state.
        "project_context",
        "project_memory_set",
        "project_memory_get",
        "project_checkpoint",
        "project_verify",
        # Interactive PTY sessions.
        "terminal_open",
        "terminal_list",
        "terminal_read",
        "terminal_write",
        "terminal_resize",
        "terminal_signal",
        "terminal_close",
        # Docker workers. Other Docker operations remain available through shell.
        "worker_run",
        "worker_list",
        "worker_exec",
        "worker_logs",
        "worker_remove",
        # Persistent command environments.
        "environment_profile_list",
        "environment_profile_set",
        "environment_profile_delete",
        "environment_profile_preview",
        # Multi-project workspace selection.
        "open_project",
        "switch_project",
        "list_projects",
        "close_project",
        "pwd",
    }
)

MINIMAL_TOOL_NAMES = frozenset(
    {
        "project_context",
        "project_verify",
        "run_command_advanced",
        "read_file",
        "read_files",
        "atomic_write_file",
        "replace_in_file",
        "replace_lines",
        "apply_patch",
        "list_files",
        "tree",
        "search_all_matches",
        "git_status",
        "git_diff",
        "git_log",
        "git_checkout",
        "git_commit",
        "git_pull",
        "git_push",
        "github_cli",
        "package_install",
        "install_project_dependencies",
        "start_process_advanced",
        "list_processes",
        "get_process_output",
        "stop_process",
        "terminal_open",
        "terminal_read",
        "terminal_write",
        "terminal_close",
        "worker_run",
        "worker_exec",
        "worker_logs",
        "worker_remove",
        "open_project",
        "switch_project",
        "list_projects",
        "pwd",
    }
)


@dataclass(frozen=True)
class ToolProfileState:
    profile: str
    all_tools: frozenset[str]
    exposed_tools: frozenset[str]
    hidden_tools: frozenset[str]
    max_exposed_tools: int


def _csv_names(environment_name: str) -> set[str]:
    return {item.strip() for item in os.getenv(environment_name, "").split(",") if item.strip()}


def resolve_tool_profile(all_tool_names: set[str] | frozenset[str]) -> ToolProfileState:
    """Resolve the configured profile without mutating the MCP registry."""
    all_tools = frozenset(all_tool_names)
    profile = os.getenv("MCP_TOOL_PROFILE", "chatgpt").strip().lower() or "chatgpt"

    if profile == "chatgpt":
        selected = set(CHATGPT_TOOL_NAMES)
    elif profile == "minimal":
        selected = set(MINIMAL_TOOL_NAMES)
    elif profile in {"full", "all"}:
        selected = set(all_tools)
        profile = "full"
    else:
        raise RuntimeError(f"Unknown MCP_TOOL_PROFILE: {profile}")

    selected.update(_csv_names("MCP_TOOL_INCLUDE"))
    selected.difference_update(_csv_names("MCP_TOOL_EXCLUDE"))

    unknown = selected - all_tools
    if unknown:
        raise RuntimeError(f"Tool profile references unknown tools: {', '.join(sorted(unknown))}")

    try:
        max_exposed = max(int(os.getenv("MCP_MAX_EXPOSED_TOOLS", "100")), 0)
    except ValueError as exc:
        raise RuntimeError("MCP_MAX_EXPOSED_TOOLS must be a non-negative integer") from exc

    if max_exposed and len(selected) > max_exposed:
        raise RuntimeError(
            f"Tool profile '{profile}' exposes {len(selected)} tools, exceeding MCP_MAX_EXPOSED_TOOLS={max_exposed}"
        )

    exposed = frozenset(selected)
    return ToolProfileState(
        profile=profile,
        all_tools=all_tools,
        exposed_tools=exposed,
        hidden_tools=all_tools - exposed,
        max_exposed_tools=max_exposed,
    )


def apply_tool_profile(mcp: Any, all_tool_names: set[str] | frozenset[str]) -> ToolProfileState:
    """Remove non-profile tools from FastMCP while preserving their Python implementations."""
    state = resolve_tool_profile(all_tool_names)
    for tool_name in sorted(state.hidden_tools):
        mcp.remove_tool(tool_name)
    return state
