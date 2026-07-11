"""MCP tools for the deployment capability group."""

from app.core import (
    READ_ONLY_ANNOTATIONS,
    _audit,
    _format_browser_result,
    _json_result,
    _run_argv,
    authorize_tool,
    json,
    mcp,
    re,
    require_scope,
    resolve_path,
    session_state,
    shutil,
    time,
    urllib,
    user_snapshot_root,
)


@mcp.tool()
def create_snapshot(name: str | None = None) -> str:
    authorize_tool("create_snapshot")
    require_scope("workspace:write")
    snapshots = user_snapshot_root()

    state = session_state()
    snapshot_id = name or f"{state.current_project_name}-{int(time.time())}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", snapshot_id):
        raise ValueError("Snapshot name may contain only letters, digits, '.', '_' and '-'")
    target = snapshots / snapshot_id
    if target.exists():
        raise FileExistsError(f"Snapshot already exists: {snapshot_id}")

    ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".venv", "dist", "build")
    shutil.copytree(state.current_project, target, ignore=ignore)

    return f"Created snapshot: {snapshot_id}"


@mcp.tool()
def restore_snapshot(snapshot_id: str) -> str:
    authorize_tool("restore_snapshot")
    require_scope("workspace:write")
    source = (user_snapshot_root() / snapshot_id).resolve()

    if not source.exists() or user_snapshot_root() not in source.parents:
        raise FileNotFoundError("Snapshot not found")

    project = session_state().current_project
    for item in project.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for item in source.iterdir():
        dest = project / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    return f"Restored snapshot: {snapshot_id}"


@mcp.tool()
def deployment_preflight(cwd: str = ".") -> str:
    """Validate a Compose project before deployment: configuration, current service state, and a snapshot identifier for rollback."""
    authorize_tool("deployment_preflight")
    require_scope("deploy:run")
    require_scope("workspace:write")
    root = resolve_path(cwd)
    if not ((root / "docker-compose.yml").exists() or (root / "compose.yml").exists()):
        raise FileNotFoundError("No docker-compose.yml or compose.yml in the selected project")
    config = _run_argv(["docker", "compose", "config", "--quiet"], root, 120)
    status = _run_argv(["docker", "compose", "ps", "--all"], root, 60)
    snapshot = create_snapshot(f"predeploy-{int(time.time())}")
    payload = {
        "project": str(root),
        "config": config,
        "status": status,
        "snapshot": snapshot,
        "ready": config["exit_code"] == 0,
    }
    _audit("deployment_preflight", {"project": str(root), "ready": payload["ready"]})
    return _json_result(payload)


@mcp.tool()
def deployment_apply(
    approval: str, services: list[str] | None = None, health_url: str | None = None, cwd: str = "."
) -> str:
    """Build and apply a Compose deployment. Requires the exact user approval phrase I_APPROVE_DEPLOYMENT and optional HTTP health evidence."""
    authorize_tool("deployment_apply")
    require_scope("deploy:run")
    require_scope("workspace:write")
    if approval != "I_APPROVE_DEPLOYMENT":
        raise PermissionError("Deployment requires explicit user approval: I_APPROVE_DEPLOYMENT")
    root = resolve_path(cwd)
    service_args = services or []
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", service) for service in service_args):
        raise ValueError("Invalid Compose service name")
    preflight = _run_argv(["docker", "compose", "config", "--quiet"], root, 120)
    if preflight["exit_code"] != 0:
        return _json_result({"deployed": False, "preflight": preflight})
    result = _run_argv(["docker", "compose", "up", "--build", "--detach", *service_args], root, 600)
    health = json.loads(wait_for_http_health(health_url)) if health_url and result["exit_code"] == 0 else None
    payload = {
        "deployed": result["exit_code"] == 0 and (health is None or health.get("healthy") is True),
        "preflight": preflight,
        "apply": result,
        "health": health,
    }
    _audit("deployment_apply", {"project": str(root), "services": service_args, "deployed": payload["deployed"]})
    return _json_result(payload)


@mcp.tool()
def deployment_rollback(snapshot_id: str, approval: str, cwd: str = ".") -> str:
    """Restore a pre-deployment snapshot and re-apply Compose. Requires exact user approval I_APPROVE_ROLLBACK."""
    authorize_tool("deployment_rollback")
    require_scope("deploy:run")
    require_scope("workspace:write")
    if approval != "I_APPROVE_ROLLBACK":
        raise PermissionError("Rollback requires explicit user approval: I_APPROVE_ROLLBACK")
    restore = restore_snapshot(snapshot_id)
    root = resolve_path(cwd)
    result = _run_argv(["docker", "compose", "up", "--build", "--detach"], root, 600)
    payload = {"restored": restore, "apply": result, "rolled_back": result["exit_code"] == 0}
    _audit(
        "deployment_rollback", {"project": str(root), "snapshot_id": snapshot_id, "rolled_back": payload["rolled_back"]}
    )
    return _json_result(payload)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def wait_for_http_health(url: str, expected_status: int = 200, timeout_seconds: int = 60) -> str:
    """Poll an HTTP endpoint until it returns the expected status or times out."""
    authorize_tool("wait_for_http_health")
    require_scope("deploy:run")
    deadline = time.monotonic() + min(max(timeout_seconds, 1), 300)
    last_status: int | None = None
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                last_status = response.status
                if last_status == expected_status:
                    return _format_browser_result({"healthy": True, "url": url, "status": last_status})
        except urllib.error.HTTPError as exc:
            last_status = exc.code
        except OSError as exc:
            last_error = str(exc)
        time.sleep(1)
    return _format_browser_result(
        {
            "healthy": False,
            "url": url,
            "expected_status": expected_status,
            "last_status": last_status,
            "last_error": last_error,
        }
    )


TOOL_EXPORTS = [
    "create_snapshot",
    "restore_snapshot",
    "deployment_preflight",
    "deployment_apply",
    "deployment_rollback",
    "wait_for_http_health",
]
