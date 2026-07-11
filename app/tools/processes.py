"""MCP tools for the processes capability group."""

from app.core import (
    MAX_OUTPUT,
    PROCESS_LOCK,
    PROCESS_LOG_LIMIT,
    READ_ONLY_ANNOTATIONS,
    ProcessRecord,
    _command_environment,
    _format_browser_result,
    _process_status,
    _read_process_output,
    authorize_tool,
    mcp,
    os,
    require_scope,
    resolve_path,
    session_state,
    shell,
    shlex,
    subprocess,
    threading,
    time,
    uuid,
)


@mcp.tool()
def start_process(command: str, cwd: str = ".", name: str | None = None) -> str:
    """Start a long-running process and capture output in the background without blocking."""
    authorize_tool("start_process")
    working_dir = resolve_path(cwd)
    process_id = (name or str(uuid.uuid4())[:8]).strip()

    if not process_id:
        process_id = str(uuid.uuid4())[:8]

    state = session_state()
    if process_id in state.processes:
        raise ValueError(f"Process id already exists: {process_id}")

    env = os.environ.copy()
    env["HOME"] = str(state.current_project)

    state.command_history.append(f"[{state.current_project_name}] {working_dir}$ {command}  # process={process_id}")

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=working_dir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        bufsize=1,
        start_new_session=True,
    )

    record = ProcessRecord(
        command=command,
        cwd=working_dir,
        process=process,
        started_at=time.time(),
    )

    with PROCESS_LOCK:
        state.processes[process_id] = record

    reader = threading.Thread(
        target=_read_process_output,
        args=(process_id, record),
        daemon=True,
        name=f"process-output-{process_id}",
    )
    reader.start()

    return "\n".join(
        [
            f"process_id: {process_id}",
            f"pid: {process.pid}",
            f"status: {_process_status(record)}",
            f"cwd: {working_dir}",
            f"command: {command}",
        ]
    )


@mcp.tool()
def list_processes() -> str:
    """List tracked background processes."""
    authorize_tool("list_processes")
    rows = []

    with PROCESS_LOCK:
        items = list(session_state().processes.items())

    for process_id, record in items:
        rows.append(
            "\n".join(
                [
                    f"process_id: {process_id}",
                    f"pid: {record.process.pid}",
                    f"status: {_process_status(record)}",
                    f"cwd: {record.cwd}",
                    f"started_at_unix: {record.started_at}",
                    f"total_output_lines: {record.total_lines}",
                    f"command: {record.command}",
                ]
            )
        )

    return "\n\n".join(rows) if rows else "No tracked processes."


@mcp.tool()
def get_process_output(process_id: str, max_lines: int = 300, since_line: int | None = None) -> str:
    """Return captured process output without blocking. Use since_line for incremental reads."""
    authorize_tool("get_process_output")
    with PROCESS_LOCK:
        record = session_state().processes.get(process_id)
        if record is None:
            raise ValueError("Unknown process_id")

        total_lines = record.total_lines
        buffered_lines = list(record.output)

    max_lines = min(max(max_lines, 1), PROCESS_LOG_LIMIT)
    first_buffered_line = max(total_lines - len(buffered_lines) + 1, 1)

    if since_line is None:
        selected = buffered_lines[-max_lines:]
        start_line = max(total_lines - len(selected) + 1, first_buffered_line)
    else:
        start_index = max(since_line - first_buffered_line, 0)
        selected = buffered_lines[start_index : start_index + max_lines]
        start_line = max(since_line, first_buffered_line)

    header = "\n".join(
        [
            f"process_id: {process_id}",
            f"pid: {record.process.pid}",
            f"status: {_process_status(record)}",
            f"exit_code: {record.process.poll()}",
            f"total_output_lines: {total_lines}",
            f"first_buffered_line: {first_buffered_line}",
            f"returned_start_line: {start_line if selected else ''}",
            f"next_since_line: {start_line + len(selected) if selected else total_lines + 1}",
        ]
    )

    numbered = "\n".join(f"{line_no}: {line}" for line_no, line in enumerate(selected, start=start_line))

    return f"{header}\n\n{numbered}"[:MAX_OUTPUT]


@mcp.tool()
def wait_for_process_output(
    process_id: str, text: str, timeout_seconds: int = 30, since_line: int | None = None
) -> str:
    """Wait until text appears in background process output, without blocking forever."""
    authorize_tool("wait_for_process_output")
    deadline = time.time() + min(max(timeout_seconds, 1), 300)
    record: ProcessRecord | None = None

    while time.time() < deadline:
        with PROCESS_LOCK:
            record = session_state().processes.get(process_id)
            if record is None:
                raise ValueError("Unknown process_id")

            total_lines = record.total_lines
            buffered_lines = list(record.output)

        first_buffered_line = max(total_lines - len(buffered_lines) + 1, 1)
        start_index = 0 if since_line is None else max(since_line - first_buffered_line, 0)

        for offset, line in enumerate(buffered_lines[start_index:], start=start_index):
            line_no = first_buffered_line + offset
            if text in line:
                return "\n".join(
                    [
                        "matched: true",
                        f"process_id: {process_id}",
                        f"line: {line_no}",
                        f"next_since_line: {line_no + 1}",
                        f"text: {line}",
                    ]
                )

        if record.process.poll() is not None and record.reader_done:
            break

        time.sleep(0.2)

    if record is None:
        raise ValueError("Unknown process_id")

    return "\n".join(
        [
            "matched: false",
            f"process_id: {process_id}",
            f"status: {_process_status(record)}",
            f"total_output_lines: {record.total_lines}",
            f"next_since_line: {record.total_lines + 1}",
        ]
    )


@mcp.tool()
def stop_process(process_id: str, kill: bool = False) -> str:
    """Stop a tracked process. Uses terminate first unless kill=True."""
    authorize_tool("stop_process")
    with PROCESS_LOCK:
        record = session_state().processes.get(process_id)

    if record is None:
        raise ValueError("Unknown process_id")

    if record.process.poll() is None:
        if kill:
            os.killpg(record.process.pid, 9)
        else:
            os.killpg(record.process.pid, 15)

        try:
            record.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(record.process.pid, 9)
            record.process.wait(timeout=5)

    status = _process_status(record)
    return f"Stopped process {process_id}: {status}"


@mcp.tool()
def forget_process(process_id: str) -> str:
    """Remove a stopped process from the tracked process list."""
    authorize_tool("forget_process")
    with PROCESS_LOCK:
        record = session_state().processes.get(process_id)

        if record is None:
            raise ValueError("Unknown process_id")

        if record.process.poll() is None:
            raise ValueError("Process is still running; stop it before forgetting it")

        del session_state().processes[process_id]

    return f"Forgot process {process_id}"


@mcp.tool()
def start_process_advanced(
    argv: list[str],
    cwd: str = ".",
    name: str | None = None,
    environment: dict[str, str] | None = None,
    secret_refs: list[str] | None = None,
    allow_stdin: bool = False,
) -> str:
    """Start an argv background process without shell parsing, with controlled environment, optional named secrets, and optional stdin forwarding."""
    authorize_tool("start_process_advanced")
    require_scope("command:run")
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise ValueError("argv must contain one or more non-empty strings")
    state = session_state()
    process_id = (name or str(uuid.uuid4())[:8]).strip() or str(uuid.uuid4())[:8]
    if process_id in state.processes:
        raise ValueError(f"Process id already exists: {process_id}")
    working_dir = resolve_path(cwd)
    command_env = _command_environment(environment, secret_refs)
    redactions = tuple(command_env[name] for name in secret_refs or [] if name in command_env)
    process = subprocess.Popen(
        argv,
        cwd=working_dir,
        env=command_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if allow_stdin else subprocess.DEVNULL,
        bufsize=1,
        start_new_session=True,
    )
    record = ProcessRecord(
        command=shlex.join(argv),
        cwd=working_dir,
        process=process,
        started_at=time.time(),
        redactions=redactions,
    )
    with PROCESS_LOCK:
        state.processes[process_id] = record
    threading.Thread(
        target=_read_process_output, args=(process_id, record), daemon=True, name=f"process-output-{process_id}"
    ).start()
    state.command_history.append(
        f"[{state.current_project_name}] {working_dir}$ {shlex.join(argv)}  # process={process_id}"
    )
    return _format_browser_result(
        {
            "process_id": process_id,
            "pid": process.pid,
            "cwd": str(working_dir),
            "argv": argv,
            "stdin_enabled": allow_stdin,
            "secret_refs_used": secret_refs or [],
        }
    )


@mcp.tool()
def send_process_input(process_id: str, data: str, append_newline: bool = True) -> str:
    """Send text to a background process started with allow_stdin=true."""
    authorize_tool("send_process_input")
    require_scope("command:run")
    record = session_state().processes.get(process_id)
    if record is None:
        raise ValueError("Unknown process_id")
    if record.process.stdin is None:
        raise ValueError("Process was not started with allow_stdin=true")
    if record.process.poll() is not None:
        raise ValueError("Process has already exited")
    record.process.stdin.write(data + ("\n" if append_newline else ""))
    record.process.stdin.flush()
    return _format_browser_result(
        {"process_id": process_id, "characters_sent": len(data), "newline_appended": append_newline}
    )


@mcp.tool()
def signal_process(process_id: str, signal_name: str = "TERM") -> str:
    """Send a POSIX signal (TERM, INT, HUP, KILL, USR1, USR2) to a tracked process group."""
    authorize_tool("signal_process")
    require_scope("command:run")
    record = session_state().processes.get(process_id)
    if record is None:
        raise ValueError("Unknown process_id")
    signals = {"TERM": 15, "INT": 2, "HUP": 1, "KILL": 9, "USR1": 10, "USR2": 12}
    signal_number = signals.get(signal_name.upper())
    if signal_number is None:
        raise ValueError(f"Unsupported signal: {signal_name}")
    if record.process.poll() is None:
        os.killpg(record.process.pid, signal_number)
    return _format_browser_result(
        {"process_id": process_id, "signal": signal_name.upper(), "status": _process_status(record)}
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def port_owner(port: int) -> str:
    """Report listeners bound to a local TCP or UDP port."""
    authorize_tool("port_owner")
    require_scope("command:run")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return shell(f"ss -lptun 'sport = :{port}' || true", cwd=".")


TOOL_EXPORTS = [
    "start_process",
    "list_processes",
    "get_process_output",
    "wait_for_process_output",
    "stop_process",
    "forget_process",
    "start_process_advanced",
    "send_process_input",
    "signal_process",
    "port_owner",
]
