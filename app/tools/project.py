"""MCP tools for the project capability group."""

from app.core import (
    AGENT_POLICY_PATH,
    AUDIT_ROOT,
    MEMORY_ROOT,
    READ_ONLY_ANNOTATIONS,
    SELF_IMPROVEMENT_WORKSPACE,
    UTC,
    Any,
    _audit,
    _identity_storage_root,
    _json_result,
    _load_project_memory,
    _project_markers,
    _run_argv,
    _save_project_memory,
    _verification_commands,
    authorize_tool,
    datetime,
    json,
    mcp,
    re,
    require_scope,
    session_state,
    shell,
    time,
    uuid,
)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def project_context(max_files: int = 120) -> str:
    """Return grounded project context: detected stack, root files, Git state, available verification suites, stored notes, and the active safety policy."""
    authorize_tool("project_context")
    require_scope("workspace:read")
    root = session_state().current_project
    markers = _project_markers(root)
    root_files = sorted(path.name for path in root.iterdir())[: min(max(max_files, 1), 500)]
    git = _run_argv(["git", "status", "--short", "--branch"], root, 30) if markers["git"] else None
    package_scripts: list[str] = []
    if (root / "package.json").exists():
        try:
            package_scripts = sorted(
                json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {}).keys()
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            package_scripts = []
    policy = (
        AGENT_POLICY_PATH.read_text(encoding="utf-8")[:12_000]
        if AGENT_POLICY_PATH.exists()
        else "No policy file configured."
    )
    memory = _load_project_memory()
    return _json_result(
        {
            "project": session_state().current_project_name,
            "path": str(root),
            "markers": markers,
            "root_files": root_files,
            "git": git,
            "package_scripts": package_scripts,
            "verification_suites": {
                name: commands for name, commands in _verification_commands(root).items() if commands
            },
            "memory_keys": sorted(memory),
            "policy": policy,
        }
    )


@mcp.tool()
def project_memory_set(key: str, value: str) -> str:
    """Persist a concise, user-approved project fact or decision outside the repository. Never store credentials or tokens here."""
    authorize_tool("project_memory_set")
    require_scope("workspace:write")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", key):
        raise ValueError("Memory key may contain only letters, digits, '.', '_' and '-'")
    if len(value) > 12_000:
        raise ValueError("Memory values are limited to 12,000 characters")
    memory = _load_project_memory()
    memory[key] = {"updated_at": datetime.now(UTC).isoformat(), "value": value}
    _save_project_memory(memory)
    _audit("project_memory_set", {"key": key, "characters": len(value)})
    return _json_result({"saved": key, "memory_keys": sorted(memory)})


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def project_memory_get(key: str | None = None) -> str:
    """Read one persistent project memory entry, or list available memory keys."""
    authorize_tool("project_memory_get")
    require_scope("workspace:read")
    memory = _load_project_memory()
    if key is None:
        return _json_result({"memory_keys": sorted(memory)})
    if key not in memory:
        raise KeyError(f"No memory entry named {key}")
    return _json_result({"key": key, "entry": memory[key]})


@mcp.tool()
def project_checkpoint(summary: str, next_steps: list[str], verification: dict[str, Any] | None = None) -> str:
    """Create a durable progress checkpoint outside the repository for long-running agent work."""
    authorize_tool("project_checkpoint")
    require_scope("workspace:write")
    if len(summary) > 12_000 or len(next_steps) > 50:
        raise ValueError("Checkpoint is too large")
    root = _identity_storage_root(MEMORY_ROOT) / "checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    checkpoint_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    payload = {
        "id": checkpoint_id,
        "at": datetime.now(UTC).isoformat(),
        "project": str(session_state().current_project),
        "summary": summary,
        "next_steps": next_steps,
        "verification": verification or {},
    }
    (root / f"{checkpoint_id}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _audit("project_checkpoint", {"checkpoint_id": checkpoint_id, "next_step_count": len(next_steps)})
    return _json_result(payload)


@mcp.tool()
def project_verify(suites: list[str] | None = None, timeout_seconds: int = 300) -> str:
    """Run detected fixed verification suites (syntax, test, lint, typecheck, build, compose) and return grounded pass/fail evidence."""
    authorize_tool("project_verify")
    require_scope("command:run")
    root = session_state().current_project
    available = _verification_commands(root)
    selected = suites or [name for name, commands in available.items() if commands]
    allowed = set(available)
    if unknown := set(selected) - allowed:
        raise ValueError(f"Unknown verification suite(s): {', '.join(sorted(unknown))}")

    results: dict[str, dict[str, Any]] = {}
    timeout = min(max(timeout_seconds, 1), 600)
    for suite in selected:
        commands = available[suite]
        if not commands:
            results[suite] = {"status": "not_configured", "commands": []}
            continue
        command_results = [_run_argv(command, root, timeout) for command in commands]
        status = (
            "passed" if all(item["exit_code"] == 0 and not item["timed_out"] for item in command_results) else "failed"
        )
        results[suite] = {"status": status, "commands": command_results}

    passed = all(result["status"] == "passed" for result in results.values())
    payload = {"project": str(root), "selected_suites": selected, "passed": passed, "results": results}
    _audit("project_verify", {"suites": selected, "passed": passed})
    return _json_result(payload)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def self_improvement_readiness() -> str:
    """Assess whether the active project is the isolated self-improvement checkout and report the branch, policy, verification, and credential prerequisites."""
    authorize_tool("self_improvement_readiness")
    require_scope("workspace:read")
    root = session_state().current_project
    is_self_workspace = root.name == SELF_IMPROVEMENT_WORKSPACE
    markers = _project_markers(root)
    git = _run_argv(["git", "status", "--short", "--branch"], root, 30) if markers["git"] else None
    origin = _run_argv(["git", "remote", "get-url", "origin"], root, 30) if markers["git"] else None
    return _json_result(
        {
            "ready": is_self_workspace and markers["git"] and AGENT_POLICY_PATH.exists(),
            "is_isolated_self_workspace": is_self_workspace,
            "expected_workspace_name": SELF_IMPROVEMENT_WORKSPACE,
            "git": git,
            "origin": origin,
            "verification_suites": [name for name, commands in _verification_commands(root).items() if commands],
            "github_pr_requirement": "Configure a fine-grained GitHub token as GITHUB_TOKEN in /config/secrets.json, then use github_push_branch and github_create_pull_request.",
            "deployment_requirement": "Use deployment_preflight and require explicit user approval before deployment_apply or deployment_rollback.",
            "policy_present": AGENT_POLICY_PATH.exists(),
        }
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def audit_events(max_entries: int = 100) -> str:
    """Return recent credential-free audit records for the authenticated identity."""
    authorize_tool("audit_events")
    require_scope("workspace:read")
    target = _identity_storage_root(AUDIT_ROOT) / "events.jsonl"
    if not target.exists():
        return _json_result({"events": []})
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-min(max(max_entries, 1), 1000) :]
    return _json_result({"events": [json.loads(line) for line in lines if line.strip()]})


@mcp.tool()
def get_project_info() -> str:
    authorize_tool("get_project_info")
    state = session_state()
    markers = {
        "git": ".git",
        "package_json": "package.json",
        "pyproject": "pyproject.toml",
        "requirements": "requirements.txt",
        "dockerfile": "Dockerfile",
        "docker_compose": "docker-compose.yml",
        "vite": "vite.config.ts",
        "next": "next.config.js",
        "pytest": "pytest.ini",
        "tsconfig": "tsconfig.json",
    }

    lines = [
        f"name: {state.current_project_name}",
        f"path: {state.current_project}",
    ]

    for key, marker in markers.items():
        lines.append(f"{key}: {(state.current_project / marker).exists()}")

    return "\n".join(lines)


@mcp.tool()
def environment_info() -> str:
    authorize_tool("environment_info")
    commands = [
        "pwd",
        "python --version || true",
        "node --version || true",
        "npm --version || true",
        "git --version || true",
        "docker --version || true",
    ]

    return shell(" && ".join(commands))


TOOL_EXPORTS = [
    "project_context",
    "project_memory_set",
    "project_memory_get",
    "project_checkpoint",
    "project_verify",
    "self_improvement_readiness",
    "audit_events",
    "get_project_info",
    "environment_info",
]
