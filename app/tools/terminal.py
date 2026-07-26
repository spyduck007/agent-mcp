"""Persistent pseudo-terminal tools for interactive shells and REPLs."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import (
    MAX_OUTPUT,
    MAX_TERMINAL_SESSIONS_PER_USER,
    TERMINAL_LOCK,
    TERMINAL_LOG_LIMIT,
    _command_environment,
    _format_browser_result,
    authorize_tool,
    mcp,
    resolve_path,
    session_state,
)


@dataclass
class TerminalRecord:
    process: subprocess.Popen[Any]
    master_fd: int
    cwd: Path
    argv: list[str]
    started_at: float
    output: str = ""
    total_chars: int = 0
    reader_done: bool = False
    last_activity_at: float = field(default_factory=time.time)


def _set_terminal_size(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _read_terminal(record: TerminalRecord) -> None:
    try:
        while True:
            try:
                data = os.read(record.master_fd, 65_536)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                raise
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            with TERMINAL_LOCK:
                record.total_chars += len(text)
                record.last_activity_at = time.time()
                record.output = (record.output + text)[-TERMINAL_LOG_LIMIT:]
    finally:
        with TERMINAL_LOCK:
            record.reader_done = True


def _terminal(session_id: str) -> TerminalRecord:
    with TERMINAL_LOCK:
        record = session_state().terminals.get(session_id)
    if record is None:
        raise ValueError("Unknown terminal session")
    record.last_activity_at = time.time()
    return record


@mcp.tool()
def terminal_open(
    argv: list[str] | None = None,
    cwd: str = ".",
    session_id: str | None = None,
    environment: dict[str, str] | None = None,
    secret_refs: list[str] | None = None,
    profile: str | None = None,
    rows: int = 40,
    cols: int = 120,
) -> str:
    """Open a persistent PTY-backed terminal suitable for shells, REPLs, debuggers, SSH, and interactive installers."""
    authorize_tool("terminal_open")
    command = argv or ["/bin/bash", "-l"]
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("argv must contain one or more non-empty strings")
    identifier = (session_id or f"term-{uuid.uuid4().hex[:8]}").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", identifier):
        raise ValueError("Invalid terminal session id")
    state = session_state()
    with TERMINAL_LOCK:
        if identifier in state.terminals:
            raise ValueError(f"Terminal session already exists: {identifier}")
        if MAX_TERMINAL_SESSIONS_PER_USER > 0 and len(state.terminals) >= MAX_TERMINAL_SESSIONS_PER_USER:
            raise RuntimeError(f"Terminal-session limit reached ({MAX_TERMINAL_SESSIONS_PER_USER})")
    working_dir = resolve_path(cwd)
    master_fd, slave_fd = pty.openpty()
    _set_terminal_size(slave_fd, rows, cols)
    try:
        process = subprocess.Popen(
            command,
            cwd=working_dir,
            env=_command_environment(environment, secret_refs, profile),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    record = TerminalRecord(process=process, master_fd=master_fd, cwd=working_dir, argv=command, started_at=time.time())
    with TERMINAL_LOCK:
        state.terminals[identifier] = record
    threading.Thread(target=_read_terminal, args=(record,), daemon=True, name=f"terminal-{identifier}").start()
    state.command_history.append(
        f"[{state.current_project_name}] {working_dir}$ PTY {command!r} # terminal={identifier}"
    )
    return _format_browser_result(
        {
            "session_id": identifier,
            "pid": process.pid,
            "cwd": str(working_dir),
            "argv": command,
            "rows": rows,
            "cols": cols,
            "profile": profile,
        }
    )


@mcp.tool()
def terminal_list() -> str:
    """List persistent terminal sessions."""
    authorize_tool("terminal_list")
    with TERMINAL_LOCK:
        sessions = [
            {
                "session_id": identifier,
                "pid": record.process.pid,
                "running": record.process.poll() is None,
                "exit_code": record.process.poll(),
                "cwd": str(record.cwd),
                "argv": record.argv,
                "started_at_unix": record.started_at,
                "total_chars": record.total_chars,
            }
            for identifier, record in session_state().terminals.items()
        ]
    return _format_browser_result({"sessions": sessions})


@mcp.tool()
def terminal_read(session_id: str, since_char: int | None = None, max_chars: int = MAX_OUTPUT) -> str:
    """Read buffered terminal output. Use next_since_char for incremental reads."""
    authorize_tool("terminal_read")
    record = _terminal(session_id)
    with TERMINAL_LOCK:
        buffered = record.output
        total = record.total_chars
        first = max(total - len(buffered), 0)
        start = first if since_char is None else max(since_char, first)
        offset = max(start - first, 0)
        limit = max(1, min(max_chars, MAX_OUTPUT))
        content = buffered[offset : offset + limit]
        running = record.process.poll() is None
        reader_done = record.reader_done
    return _format_browser_result(
        {
            "session_id": session_id,
            "running": running,
            "exit_code": record.process.poll(),
            "reader_done": reader_done,
            "first_buffered_char": first,
            "returned_start_char": start,
            "next_since_char": start + len(content),
            "total_chars": total,
            "output": content,
        }
    )


@mcp.tool()
def terminal_write(session_id: str, data: str, append_newline: bool = False) -> str:
    """Write text to a persistent terminal session."""
    authorize_tool("terminal_write")
    record = _terminal(session_id)
    if record.process.poll() is not None:
        raise ValueError("Terminal process has exited")
    payload = (data + ("\n" if append_newline else "")).encode()
    written = os.write(record.master_fd, payload)
    record.last_activity_at = time.time()
    return _format_browser_result({"session_id": session_id, "bytes_written": written})


@mcp.tool()
def terminal_resize(session_id: str, rows: int, cols: int) -> str:
    """Resize a persistent terminal."""
    authorize_tool("terminal_resize")
    record = _terminal(session_id)
    _set_terminal_size(record.master_fd, rows, cols)
    return _format_browser_result({"session_id": session_id, "rows": rows, "cols": cols})


@mcp.tool()
def terminal_signal(session_id: str, signal_name: str = "SIGINT") -> str:
    """Send a Unix signal to the terminal process group."""
    authorize_tool("terminal_signal")
    record = _terminal(session_id)
    normalized = signal_name.upper()
    if not normalized.startswith("SIG"):
        normalized = f"SIG{normalized}"
    signum = getattr(signal, normalized, None)
    if not isinstance(signum, int):
        raise ValueError(f"Unknown signal: {signal_name}")
    if record.process.poll() is None:
        os.killpg(record.process.pid, signum)
    return _format_browser_result({"session_id": session_id, "signal": normalized})


@mcp.tool()
def terminal_close(session_id: str, kill: bool = False) -> str:
    """Close a terminal and stop its process group."""
    authorize_tool("terminal_close")
    record = _terminal(session_id)
    if record.process.poll() is None:
        os.killpg(record.process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        try:
            record.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(record.process.pid, signal.SIGKILL)
            record.process.wait(timeout=5)
    try:
        os.close(record.master_fd)
    except OSError:
        pass
    with TERMINAL_LOCK:
        session_state().terminals.pop(session_id, None)
    return _format_browser_result({"session_id": session_id, "exit_code": record.process.poll(), "closed": True})


TOOL_EXPORTS = [
    "terminal_open",
    "terminal_list",
    "terminal_read",
    "terminal_write",
    "terminal_resize",
    "terminal_signal",
    "terminal_close",
]
