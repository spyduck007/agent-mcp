"""MCP tools for the commands capability group."""

from app.core import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_OUTPUT,
    _command_environment,
    _format_browser_result,
    _redact_text,
    authorize_tool,
    mcp,
    require_scope,
    resolve_path,
    session_state,
    shell,
    shlex,
    subprocess,
)


@mcp.tool()
def run_command(command: str, cwd: str = ".", timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    authorize_tool("run_command")
    return shell(command, cwd, timeout_seconds)


@mcp.tool()
def command_history(max_entries: int = 50) -> str:
    authorize_tool("command_history")
    return "\n".join(session_state().command_history[-max_entries:])


@mcp.tool()
def run_command_advanced(
    argv: list[str],
    cwd: str = ".",
    environment: dict[str, str] | None = None,
    secret_refs: list[str] | None = None,
    stdin: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_file: str | None = None,
) -> str:
    """Run an argv command without shell parsing. Supports controlled environment values, named secret injection, stdin, timeout, and optional captured-output file. Secret values are never returned by this tool."""
    authorize_tool("run_command_advanced")
    require_scope("command:run")
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise ValueError("argv must contain one or more non-empty strings")
    working_dir = resolve_path(cwd)
    timeout = min(max(timeout_seconds, 1), 300)
    state = session_state()
    state.command_history.append(f"[{state.current_project_name}] {working_dir}$ {shlex.join(argv)}")
    command_env = _command_environment(environment, secret_refs)
    redactions = [command_env[name] for name in secret_refs or [] if name in command_env]
    try:
        result = subprocess.run(
            argv,
            cwd=working_dir,
            env=command_env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        result = None
        timed_out = True
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    else:
        stdout = result.stdout
        stderr = result.stderr

    stdout = _redact_text(stdout, redactions)
    stderr = _redact_text(stderr, redactions)
    combined = f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
    output_path = None
    if output_file:
        require_scope("workspace:write")
        target = resolve_path(output_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(combined, encoding="utf-8")
        output_path = str(target)
    return _format_browser_result(
        {
            "argv": argv,
            "cwd": str(working_dir),
            "exit_code": None if result is None else result.returncode,
            "timed_out": timed_out,
            "timeout_seconds": timeout,
            "stdout": stdout[:MAX_OUTPUT],
            "stderr": stderr[:MAX_OUTPUT],
            "output_file": output_path,
            "secret_refs_used": secret_refs or [],
        }
    )


TOOL_EXPORTS = ["run_command", "command_history", "run_command_advanced"]
