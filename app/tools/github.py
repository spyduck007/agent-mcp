"""MCP tools for the github capability group."""

from app.core import (
    _audit,
    _json_result,
    _run_argv,
    _secret_values,
    authorize_tool,
    base64,
    json,
    mcp,
    re,
    require_scope,
    resolve_path,
    urllib,
)


@mcp.tool()
def github_push_branch(branch: str, secret_ref: str = "GITHUB_TOKEN", cwd: str = ".") -> str:
    """Push a branch using an ephemeral fine-grained GitHub token reference; the token is never persisted in Git config or returned."""
    authorize_tool("github_push_branch")
    require_scope("github:write")
    require_scope("workspace:write")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,180}", branch):
        raise ValueError("Invalid branch name")
    token = _secret_values([secret_ref])[secret_ref]
    root = resolve_path(cwd)
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    result = _run_argv(
        [
            "git",
            "-c",
            f"http.https://github.com/.extraheader=AUTHORIZATION: Basic {auth}",
            "push",
            "--set-upstream",
            "origin",
            branch,
        ],
        root,
        300,
    )
    safe_result = dict(result)
    safe_result["argv"] = ["git", "push", "--set-upstream", "origin", branch]
    _audit("github_push_branch", {"branch": branch, "exit_code": result["exit_code"]})
    return _json_result({"branch": branch, "result": safe_result, "secret_ref_used": secret_ref})


@mcp.tool()
def github_create_pull_request(
    repository: str, head: str, title: str, body: str, base: str = "main", secret_ref: str = "GITHUB_TOKEN"
) -> str:
    """Create a GitHub pull request with an ephemeral fine-grained token. Requires repository format owner/name and github:write."""
    authorize_tool("github_create_pull_request")
    require_scope("github:write")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    token = _secret_values([secret_ref])[secret_ref]
    payload = json.dumps({"title": title[:256], "head": head, "base": base, "body": body[:60_000]}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pulls",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read(200_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        result = {"status": exc.code, "error": exc.read(100_000).decode("utf-8", errors="replace")}
        _audit("github_create_pull_request", {"repository": repository, "head": head, "status": exc.code})
        return _json_result(result)
    safe = {key: result.get(key) for key in ("number", "html_url", "state", "title", "head", "base")}
    _audit("github_create_pull_request", {"repository": repository, "head": head, "number": result.get("number")})
    return _json_result(safe)


TOOL_EXPORTS = ["github_push_branch", "github_create_pull_request"]


@mcp.tool()
def github_cli(args: list[str], cwd: str = ".", timeout_seconds: int = 3600) -> str:
    """Run an arbitrary authenticated GitHub CLI command using the persistent root profile."""
    authorize_tool("github_cli")
    if not args or not all(isinstance(part, str) and part for part in args):
        raise ValueError("args must contain one or more non-empty strings")
    root = resolve_path(cwd)
    return _json_result(_run_argv(["gh", *args], root, timeout_seconds))


@mcp.tool()
def github_clone(repository: str, destination: str, branch: str | None = None) -> str:
    """Clone a GitHub repository into the workspace using gh authentication."""
    authorize_tool("github_clone")
    target = resolve_path(destination)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"Destination is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = ["gh", "repo", "clone", repository, str(target)]
    if branch:
        argv.extend(["--", "--branch", branch])
    return _json_result(_run_argv(argv, target.parent, 3600))


@mcp.tool()
def github_merge_pull_request(
    repository: str,
    pull_request: str,
    method: str = "squash",
    delete_branch: bool = True,
) -> str:
    """Merge a GitHub pull request using merge, squash, or rebase."""
    authorize_tool("github_merge_pull_request")
    if method not in {"merge", "squash", "rebase"}:
        raise ValueError("method must be merge, squash, or rebase")
    argv = ["gh", "pr", "merge", pull_request, f"--{method}", "--repo", repository]
    if delete_branch:
        argv.append("--delete-branch")
    return _json_result(_run_argv(argv, resolve_path("."), 600))


@mcp.tool()
def github_workflow_run(
    repository: str,
    workflow: str,
    ref: str | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """Dispatch a GitHub Actions workflow."""
    authorize_tool("github_workflow_run")
    argv = ["gh", "workflow", "run", workflow, "--repo", repository]
    if ref:
        argv.extend(["--ref", ref])
    for key, value in (fields or {}).items():
        argv.extend(["--field", f"{key}={value}"])
    return _json_result(_run_argv(argv, resolve_path("."), 300))


TOOL_EXPORTS.extend(["github_cli", "github_clone", "github_merge_pull_request", "github_workflow_run"])
