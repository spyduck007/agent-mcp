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
