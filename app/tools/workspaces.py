"""MCP tools for the workspaces capability group."""

from app.core import (
    READ_ONLY_ANNOTATIONS,
    authorize_tool,
    mcp,
    resolve_path,
    session_state,
    set_current_project,
)


@mcp.tool()
def open_project(path: str, name: str | None = None) -> str:
    """Open a project directory and make it the active project."""
    authorize_tool("open_project")
    target = resolve_path(path)

    if not target.exists():
        raise FileNotFoundError(path)

    if not target.is_dir():
        raise NotADirectoryError(path)

    project_name = name or target.name or "project"
    set_current_project(project_name, target)

    return f"Opened project '{project_name}' at {target}"


@mcp.tool()
def switch_project(name: str) -> str:
    """Switch to a previously opened project."""
    authorize_tool("switch_project")
    state = session_state()
    if name not in state.projects:
        raise ValueError(f"Unknown project: {name}")

    set_current_project(name, state.projects[name])
    return f"Switched to project '{name}' at {state.current_project}"


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_projects() -> str:
    """List opened projects."""
    authorize_tool("list_projects")
    state = session_state()
    if not state.projects:
        return "No projects opened."

    return "\n".join(
        f"{'*' if name == state.current_project_name else ' '} {name}: {path}" for name, path in state.projects.items()
    )


@mcp.tool()
def close_project(name: str) -> str:
    """Forget an opened project."""
    authorize_tool("close_project")
    state = session_state()

    if name not in state.projects:
        raise ValueError(f"Unknown project: {name}")

    if name == state.current_project_name and len(state.projects) == 1:
        raise ValueError("Cannot close the last workspace project")

    del state.projects[name]

    if name == state.current_project_name:
        state.current_project_name, state.current_project = next(iter(state.projects.items()))

    return f"Closed project: {name}"


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def pwd() -> str:
    """Return the active project directory."""
    authorize_tool("pwd")
    state = session_state()
    return f"{state.current_project_name}: {state.current_project}"


TOOL_EXPORTS = ["open_project", "switch_project", "list_projects", "close_project", "pwd"]
