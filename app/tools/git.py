"""MCP tools for the git capability group."""

from app.core import (
    authorize_tool,
    mcp,
    require_scope,
    resolve_path,
    shell,
    shlex,
    subprocess,
)


@mcp.tool()
def apply_patch(patch: str, cwd: str = ".") -> str:
    authorize_tool("apply_patch")
    require_scope("workspace:write")
    working_dir = resolve_path(cwd)

    process = subprocess.run(
        ["patch", "-p1"],
        input=patch,
        cwd=working_dir,
        text=True,
        capture_output=True,
        timeout=30,
    )

    return f"exit_code: {process.returncode}\n\nstdout:\n{process.stdout}\n\nstderr:\n{process.stderr}"


@mcp.tool()
def git_status(cwd: str = ".") -> str:
    authorize_tool("git_status")
    return shell("git status --short", cwd)


@mcp.tool()
def git_diff(cwd: str = ".") -> str:
    authorize_tool("git_diff")
    return shell("git diff", cwd)


@mcp.tool()
def git_log(cwd: str = ".", max_count: int = 10) -> str:
    authorize_tool("git_log")
    return shell(f"git log --oneline -n {max_count}", cwd)


@mcp.tool()
def git_branch(cwd: str = ".") -> str:
    authorize_tool("git_branch")
    return shell("git branch --show-current && git branch", cwd)


@mcp.tool()
def git_checkout(branch: str, create: bool = False, cwd: str = ".") -> str:
    authorize_tool("git_checkout")
    safe_branch = shlex.quote(branch)
    command = f"git checkout {'-b ' if create else ''}{safe_branch}"
    return shell(command, cwd)


@mcp.tool()
def git_commit(message: str, cwd: str = ".") -> str:
    authorize_tool("git_commit")
    safe_message = shlex.quote(message)
    return shell(f"git add . && git commit -m {safe_message}", cwd)


@mcp.tool()
def git_restore(path: str = ".", cwd: str = ".") -> str:
    authorize_tool("git_restore")
    safe_path = shlex.quote(path)
    return shell(f"git restore {safe_path}", cwd)


@mcp.tool()
def git_staged_diff(cwd: str = ".") -> str:
    authorize_tool("git_staged_diff")
    return shell("git diff --cached", cwd)


@mcp.tool()
def git_show(revision: str = "HEAD", cwd: str = ".") -> str:
    authorize_tool("git_show")
    return shell(f"git show --stat --patch {shlex.quote(revision)}", cwd)


@mcp.tool()
def git_blame(file_path: str, start_line: int | None = None, end_line: int | None = None, cwd: str = ".") -> str:
    authorize_tool("git_blame")
    line_range = "" if start_line is None else f" -L {max(start_line, 1)},{max(end_line or start_line, start_line)}"
    return shell(f"git blame{line_range} -- {shlex.quote(file_path)}", cwd)


@mcp.tool()
def git_fetch(remote: str = "origin", cwd: str = ".") -> str:
    authorize_tool("git_fetch")
    return shell(f"git fetch {shlex.quote(remote)} --prune", cwd)


@mcp.tool()
def git_pull(remote: str = "origin", branch: str | None = None, rebase: bool = True, cwd: str = ".") -> str:
    authorize_tool("git_pull")
    require_scope("workspace:write")
    branch_part = f" {shlex.quote(branch)}" if branch else ""
    return shell(f"git pull {'--rebase ' if rebase else ''}{shlex.quote(remote)}{branch_part}", cwd)


@mcp.tool()
def git_push(remote: str = "origin", branch: str | None = None, set_upstream: bool = False, cwd: str = ".") -> str:
    authorize_tool("git_push")
    require_scope("workspace:write")
    branch_part = f" {shlex.quote(branch)}" if branch else ""
    return shell(f"git push {'-u ' if set_upstream else ''}{shlex.quote(remote)}{branch_part}", cwd)


@mcp.tool()
def git_stash(action: str = "push", message: str | None = None, cwd: str = ".") -> str:
    authorize_tool("git_stash")
    require_scope("workspace:write")
    if action not in {"push", "pop", "list", "drop"}:
        raise ValueError("action must be push, pop, list, or drop")
    extra = f" -m {shlex.quote(message)}" if action == "push" and message else ""
    return shell(f"git stash {action}{extra}", cwd)


@mcp.tool()
def git_worktree(action: str, path: str | None = None, branch: str | None = None, cwd: str = ".") -> str:
    """List, add, or remove Git worktrees. Added paths must be in the assigned workspace."""
    authorize_tool("git_worktree")
    require_scope("workspace:write")
    if action == "list":
        return shell("git worktree list --porcelain", cwd)
    if action == "add":
        if not path or not branch:
            raise ValueError("add requires path and branch")
        target = resolve_path(path)
        return shell(f"git worktree add {shlex.quote(str(target))} {shlex.quote(branch)}", cwd)
    if action == "remove":
        if not path:
            raise ValueError("remove requires path")
        target = resolve_path(path)
        return shell(f"git worktree remove {shlex.quote(str(target))}", cwd)
    raise ValueError("action must be list, add, or remove")


TOOL_EXPORTS = [
    "apply_patch",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_checkout",
    "git_commit",
    "git_restore",
    "git_staged_diff",
    "git_show",
    "git_blame",
    "git_fetch",
    "git_pull",
    "git_push",
    "git_stash",
    "git_worktree",
]
