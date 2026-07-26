"""Docker-backed worker containers for isolated heavy toolchains."""

from __future__ import annotations

import json
import re
from typing import Any

from app.core import (
    MAX_OUTPUT,
    _command_environment,
    _format_browser_result,
    _run_argv,
    _secret_values,
    authorize_tool,
    mcp,
    resolve_path,
    session_state,
)


def _container_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", name):
        raise ValueError("Invalid container name")
    return name


def _mount_spec(source: str, destination: str, read_only: bool) -> str:
    resolved = resolve_path(source)
    suffix = ":ro" if read_only else ""
    return f"{resolved}:{destination}{suffix}"


@mcp.tool()
def worker_run(
    image: str,
    argv: list[str] | None = None,
    name: str | None = None,
    cwd: str = ".",
    workspace_destination: str = "/workspace",
    environment: dict[str, str] | None = None,
    secret_refs: list[str] | None = None,
    volumes: list[dict[str, Any]] | None = None,
    ports: list[str] | None = None,
    devices: list[str] | None = None,
    privileged: bool = False,
    network: str | None = None,
    gpus: str | None = None,
    detach: bool = False,
    remove: bool = True,
    interactive: bool = False,
    timeout_seconds: int = 3600,
) -> str:
    """Run an arbitrary Docker worker with workspace, volumes, devices, networking, GPU, and privilege controls."""
    authorize_tool("worker_run")
    if not image.strip():
        raise ValueError("image is required")
    working_dir = resolve_path(cwd)
    command = ["docker", "run"]
    if remove and not detach:
        command.append("--rm")
    if detach:
        command.append("--detach")
    if interactive:
        command.append("--interactive")
    if name:
        command.extend(["--name", _container_name(name)])
    if privileged:
        command.append("--privileged")
    if network:
        command.extend(["--network", network])
    if gpus:
        command.extend(["--gpus", gpus])
    command.extend(["--volume", f"{working_dir}:{workspace_destination}", "--workdir", workspace_destination])
    for volume in volumes or []:
        source = str(volume.get("source", ""))
        destination = str(volume.get("destination", ""))
        if not source or not destination.startswith("/"):
            raise ValueError("Each volume requires source and absolute destination")
        command.extend(["--volume", _mount_spec(source, destination, bool(volume.get("read_only", False)))])
    for port in ports or []:
        command.extend(["--publish", port])
    for device in devices or []:
        command.extend(["--device", device])
    command_env = _command_environment(environment=environment, secret_refs=secret_refs)
    secret_values = _secret_values(secret_refs or [])
    keys = sorted(set(environment or {}) | set(secret_values))
    for key in keys:
        command.extend(["--env", key])
    command.append(image)
    command.extend(argv or [])
    result = _run_argv_with_env(command, working_dir, timeout_seconds, command_env)
    safe = dict(result)
    safe["argv"] = [part if part not in secret_values.values() else "[REDACTED]" for part in result["argv"]]
    return _format_browser_result(safe)


def _run_argv_with_env(argv: list[str], cwd, timeout_seconds: int, environment: dict[str, str]) -> dict[str, Any]:
    import subprocess

    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            timeout=None if timeout_seconds <= 0 else timeout_seconds,
        )
        return {
            "argv": argv,
            "exit_code": result.returncode,
            "timed_out": False,
            "stdout": result.stdout[:MAX_OUTPUT],
            "stderr": result.stderr[:MAX_OUTPUT],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "exit_code": None,
            "timed_out": True,
            "stdout": (exc.stdout or "")[:MAX_OUTPUT],
            "stderr": (exc.stderr or "")[:MAX_OUTPUT],
        }


@mcp.tool()
def worker_list(all_containers: bool = True) -> str:
    """List Docker containers with bounded structured output."""
    authorize_tool("worker_list")
    argv = ["docker", "ps", "--format", "{{json .}}"]
    if all_containers:
        argv.insert(2, "--all")
    result = _run_argv(argv, session_state().current_project, 60)
    records = []
    for line in result.get("stdout", "").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"raw": line})
    return _format_browser_result({"exit_code": result["exit_code"], "containers": records, "stderr": result["stderr"]})


@mcp.tool()
def worker_exec(
    container: str,
    argv: list[str],
    environment: dict[str, str] | None = None,
    secret_refs: list[str] | None = None,
    interactive: bool = False,
    timeout_seconds: int = 3600,
) -> str:
    """Execute an argv command inside an existing Docker worker."""
    authorize_tool("worker_exec")
    if not argv:
        raise ValueError("argv is required")
    command = ["docker", "exec"]
    if interactive:
        command.append("--interactive")
    values = _secret_values(secret_refs or [])
    command_env = _command_environment(environment=environment, secret_refs=secret_refs)
    for key in sorted(set(environment or {}) | set(values)):
        command.extend(["--env", key])
    command.extend([_container_name(container), *argv])
    return _format_browser_result(
        _run_argv_with_env(command, session_state().current_project, timeout_seconds, command_env)
    )


@mcp.tool()
def worker_logs(container: str, tail: int = 1000, since: str | None = None) -> str:
    """Read logs from a Docker worker."""
    authorize_tool("worker_logs")
    argv = ["docker", "logs", "--tail", str(max(1, min(tail, 100_000)))]
    if since:
        argv.extend(["--since", since])
    argv.append(_container_name(container))
    return _format_browser_result(_run_argv(argv, session_state().current_project, 120))


@mcp.tool()
def worker_stop(container: str, timeout_seconds: int = 10) -> str:
    """Stop a Docker worker."""
    authorize_tool("worker_stop")
    return _format_browser_result(
        _run_argv(
            ["docker", "stop", "--time", str(max(timeout_seconds, 0)), _container_name(container)],
            session_state().current_project,
            120,
        )
    )


@mcp.tool()
def worker_remove(container: str, force: bool = False, volumes: bool = False) -> str:
    """Remove a Docker worker."""
    authorize_tool("worker_remove")
    argv = ["docker", "rm"]
    if force:
        argv.append("--force")
    if volumes:
        argv.append("--volumes")
    argv.append(_container_name(container))
    return _format_browser_result(_run_argv(argv, session_state().current_project, 120))


@mcp.tool()
def worker_copy(source: str, destination: str) -> str:
    """Copy files between a worker and the active workspace using Docker cp syntax."""
    authorize_tool("worker_copy")
    # At least one side must resolve inside the workspace; the other may use container:path syntax.
    if ":" not in source:
        source = str(resolve_path(source))
    if ":" not in destination:
        destination = str(resolve_path(destination))
    return _format_browser_result(
        _run_argv(["docker", "cp", source, destination], session_state().current_project, 300)
    )


@mcp.tool()
def worker_pull(image: str) -> str:
    """Pull a Docker image."""
    authorize_tool("worker_pull")
    return _format_browser_result(_run_argv(["docker", "pull", image], session_state().current_project, 3600))


@mcp.tool()
def worker_build(
    tag: str,
    context: str = ".",
    dockerfile: str | None = None,
    build_args: dict[str, str] | None = None,
    no_cache: bool = False,
    pull: bool = False,
    timeout_seconds: int = 0,
) -> str:
    """Build a Docker worker image from a workspace context."""
    authorize_tool("worker_build")
    context_path = resolve_path(context)
    argv = ["docker", "build", "--tag", tag]
    if dockerfile:
        argv.extend(["--file", str(resolve_path(dockerfile))])
    if no_cache:
        argv.append("--no-cache")
    if pull:
        argv.append("--pull")
    for key, value in (build_args or {}).items():
        argv.extend(["--build-arg", f"{key}={value}"])
    argv.append(str(context_path))
    return _format_browser_result(_run_argv(argv, session_state().current_project, timeout_seconds or 86_400))


TOOL_EXPORTS = [
    "worker_run",
    "worker_list",
    "worker_exec",
    "worker_logs",
    "worker_stop",
    "worker_remove",
    "worker_copy",
    "worker_pull",
    "worker_build",
]
