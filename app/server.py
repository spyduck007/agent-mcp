import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "coding-agent-mcp",
    host="0.0.0.0",
    port=8080,
    streamable_http_path="/mcp",
)

HOST_ROOT = Path("/host").resolve()
SNAPSHOT_ROOT = Path("/snapshots").resolve()

PROJECTS: dict[str, Path] = {}
CURRENT_PROJECT_NAME = "host"
CURRENT_PROJECT = HOST_ROOT

PROCESS_LOG_LIMIT = 5000
BROWSER_LOG_LIMIT = 500


@dataclass
class ProcessRecord:
    command: str
    cwd: Path
    process: subprocess.Popen
    started_at: float
    output: deque[str] = field(default_factory=lambda: deque(maxlen=PROCESS_LOG_LIMIT))
    total_lines: int = 0
    reader_done: bool = False


@dataclass
class BrowserSessionRecord:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    created_at: float
    console_messages: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=BROWSER_LOG_LIMIT))
    page_errors: deque[str] = field(default_factory=lambda: deque(maxlen=BROWSER_LOG_LIMIT))
    failed_requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=BROWSER_LOG_LIMIT))
    responses: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=BROWSER_LOG_LIMIT))


PROCESSES: dict[str, ProcessRecord] = {}
BROWSER_SESSIONS: dict[str, BrowserSessionRecord] = {}
PROCESS_LOCK = threading.Lock()
COMMAND_HISTORY: list[str] = []

MAX_READ_BYTES = 500_000
MAX_OUTPUT = 80_000
DEFAULT_TIMEOUT_SECONDS = 30


def set_current_project(name: str, path: Path) -> None:
    global CURRENT_PROJECT_NAME, CURRENT_PROJECT
    PROJECTS[name] = path.resolve()
    CURRENT_PROJECT_NAME = name
    CURRENT_PROJECT = path.resolve()


def resolve_path(path: str = ".") -> Path:
    raw = Path(path).expanduser()

    if raw.is_absolute():
        target = raw.resolve()
    else:
        target = (CURRENT_PROJECT / raw).resolve()

    return target


def shell(command: str, cwd: str = ".", timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    working_dir = Path(cwd).resolve() if cwd.startswith("/") else resolve_path(cwd)
    timeout = min(max(timeout_seconds, 1), 300)

    env = os.environ.copy()
    env["HOME"] = str(HOST_ROOT)

    COMMAND_HISTORY.append(f"[{CURRENT_PROJECT_NAME}] {working_dir}$ {command}")

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return f"timed_out: true\nseconds: {timeout}\n\n{output[:MAX_OUTPUT]}"

    output = f"exit_code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    return output[:MAX_OUTPUT]


@mcp.tool()
def open_project(path: str, name: Optional[str] = None) -> str:
    """Open a project directory and make it the active project."""
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
    if name not in PROJECTS:
        raise ValueError(f"Unknown project: {name}")

    set_current_project(name, PROJECTS[name])
    return f"Switched to project '{name}' at {CURRENT_PROJECT}"


@mcp.tool()
def list_projects() -> str:
    """List opened projects."""
    if not PROJECTS:
        return "No projects opened."

    return "\n".join(
        f"{'*' if name == CURRENT_PROJECT_NAME else ' '} {name}: {path}"
        for name, path in PROJECTS.items()
    )


@mcp.tool()
def close_project(name: str) -> str:
    """Forget an opened project."""
    global CURRENT_PROJECT_NAME, CURRENT_PROJECT

    if name not in PROJECTS:
        raise ValueError(f"Unknown project: {name}")

    del PROJECTS[name]

    if name == CURRENT_PROJECT_NAME:
        CURRENT_PROJECT_NAME = "host"
        CURRENT_PROJECT = HOST_ROOT

    return f"Closed project: {name}"


@mcp.tool()
def pwd() -> str:
    """Return the active project directory."""
    return f"{CURRENT_PROJECT_NAME}: {CURRENT_PROJECT}"


@mcp.tool()
def write_file(file_path: str, contents: str) -> str:
    target = resolve_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    return f"Wrote {len(contents)} characters to {target}"


@mcp.tool()
def append_file(file_path: str, contents: str) -> str:
    target = resolve_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("a", encoding="utf-8") as f:
        f.write(contents)

    return f"Appended {len(contents)} characters to {target}"


@mcp.tool()
def read_file(file_path: str, start_line: int = 1, max_lines: int = 300) -> str:
    target = resolve_path(file_path)

    if not target.exists():
        raise FileNotFoundError(file_path)

    data = target.read_bytes()[:MAX_READ_BYTES]
    lines = data.decode("utf-8", errors="replace").splitlines()

    start = max(start_line - 1, 0)
    selected = lines[start:start + max_lines]

    return "\n".join(
        f"{line_no}: {line}"
        for line_no, line in enumerate(selected, start=start + 1)
    )


@mcp.tool()
def read_files(file_paths: list[str]) -> str:
    return "\n\n".join(
        f"--- {path} ---\n{read_file(path)}"
        for path in file_paths
    )


@mcp.tool()
def replace_in_file(
    file_path: str,
    old_text: str,
    new_text: str,
    expected_replacements: Optional[int] = None,
) -> str:
    target = resolve_path(file_path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)

    if count == 0:
        raise ValueError("old_text was not found")

    if expected_replacements is not None and count != expected_replacements:
        raise ValueError(f"Expected {expected_replacements}, found {count}")

    target.write_text(text.replace(old_text, new_text), encoding="utf-8")
    return f"Replaced {count} occurrence(s) in {target}"


@mcp.tool()
def replace_lines(file_path: str, start_line: int, end_line: int, new_content: str) -> str:
    target = resolve_path(file_path)
    lines = target.read_text(encoding="utf-8").splitlines()

    before = lines[:start_line - 1]
    after = lines[end_line:]
    replacement = new_content.splitlines()

    target.write_text("\n".join(before + replacement + after) + "\n", encoding="utf-8")
    return f"Replaced lines {start_line}-{end_line} in {target}"


@mcp.tool()
def insert_at_line(file_path: str, line_number: int, content: str) -> str:
    target = resolve_path(file_path)
    lines = target.read_text(encoding="utf-8").splitlines()

    index = max(min(line_number - 1, len(lines)), 0)
    lines[index:index] = content.splitlines()

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"Inserted content at line {line_number} in {target}"


@mcp.tool()
def copy_path(source: str, destination: str) -> str:
    src = resolve_path(source)
    dst = resolve_path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)

    return f"Copied {src} to {dst}"


@mcp.tool()
def move_path(source: str, destination: str) -> str:
    src = resolve_path(source)
    dst = resolve_path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} to {dst}"


@mcp.tool()
def delete_path(path: str) -> str:
    target = resolve_path(path)

    if target.is_dir():
        shutil.rmtree(target)
        return f"Deleted directory: {target}"

    target.unlink()
    return f"Deleted file: {target}"


@mcp.tool()
def create_directory(directory: str) -> str:
    target = resolve_path(directory)
    target.mkdir(parents=True, exist_ok=True)
    return f"Created directory: {target}"


@mcp.tool()
def list_files(directory: str = ".", recursive: bool = True, max_entries: int = 800) -> str:
    root = resolve_path(directory)
    iterator = root.rglob("*") if recursive else root.iterdir()

    entries = []
    for path in iterator:
        if len(entries) >= max_entries:
            entries.append("... output truncated ...")
            break

        try:
            rel = path.relative_to(CURRENT_PROJECT)
        except ValueError:
            rel = path

        entries.append(f"{rel}{'/' if path.is_dir() else ''}")

    return "\n".join(entries)


@mcp.tool()
def tree(directory: str = ".", depth: int = 3, max_entries: int = 400) -> str:
    root = resolve_path(directory)
    output = [f"{root}/"]
    count = 0

    def walk(path: Path, prefix: str, current_depth: int):
        nonlocal count

        if current_depth > depth:
            return

        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))

        for index, entry in enumerate(entries):
            if count >= max_entries:
                output.append("... output truncated ...")
                return

            connector = "└── " if index == len(entries) - 1 else "├── "
            output.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            count += 1

            if entry.is_dir() and entry.name not in {"node_modules", ".git", ".venv", "__pycache__"}:
                extension = "    " if index == len(entries) - 1 else "│   "
                walk(entry, prefix + extension, current_depth + 1)

    walk(root, "", 1)
    return "\n".join(output)


@mcp.tool()
def find_file(pattern: str, directory: str = ".", max_results: int = 200) -> str:
    root = resolve_path(directory)
    results = []

    for path in root.rglob(pattern):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break
        results.append(str(path))

    return "\n".join(results)


@mcp.tool()
def search_files(query: str, directory: str = ".", max_results: int = 200) -> str:
    root = resolve_path(directory)
    results = []

    for path in root.rglob("*"):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break

        if not path.is_file() or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line_no, line in enumerate(lines, start=1):
            if query in line:
                results.append(f"{path}:{line_no}: {line}")
                break

    return "\n".join(results)


@mcp.tool()
def regex_search(pattern: str, directory: str = ".", max_results: int = 200) -> str:
    root = resolve_path(directory)
    rx = re.compile(pattern)
    results = []

    for path in root.rglob("*"):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break

        if not path.is_file() or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line_no, line in enumerate(lines, start=1):
            if rx.search(line):
                results.append(f"{path}:{line_no}: {line}")
                break

    return "\n".join(results)


@mcp.tool()
def find_symbol(name: str, directory: str = ".", max_results: int = 100) -> str:
    pattern = rf"^\s*(def|class|function|const|let|var|async function|export function|export class)\s+{re.escape(name)}\b"
    return regex_search(pattern, directory, max_results)


@mcp.tool()
def stat_path(path: str) -> str:
    target = resolve_path(path)
    stat = target.stat()

    return "\n".join([
        f"path: {target}",
        f"type: {'directory' if target.is_dir() else 'file'}",
        f"size_bytes: {stat.st_size}",
        f"modified_time_unix: {stat.st_mtime}",
    ])


@mcp.tool()
def run_command(command: str, cwd: str = ".", timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    return shell(command, cwd, timeout_seconds)


@mcp.tool()
def command_history(max_entries: int = 50) -> str:
    return "\n".join(COMMAND_HISTORY[-max_entries:])


def _process_status(record: ProcessRecord) -> str:
    status = record.process.poll()
    return "running" if status is None else f"exited {status}"


def _record_process_line(record: ProcessRecord, line: str) -> None:
    with PROCESS_LOCK:
        record.total_lines += 1
        record.output.append(line.rstrip("\n"))


def _read_process_output(process_id: str, record: ProcessRecord) -> None:
    try:
        if record.process.stdout is None:
            return

        for line in iter(record.process.stdout.readline, ""):
            if line == "":
                break
            _record_process_line(record, line)
    finally:
        with PROCESS_LOCK:
            record.reader_done = True


@mcp.tool()
def start_process(command: str, cwd: str = ".", name: Optional[str] = None) -> str:
    """Start a long-running process and capture output in the background without blocking."""
    working_dir = resolve_path(cwd)
    process_id = (name or str(uuid.uuid4())[:8]).strip()

    if not process_id:
        process_id = str(uuid.uuid4())[:8]

    if process_id in PROCESSES:
        raise ValueError(f"Process id already exists: {process_id}")

    env = os.environ.copy()
    env["HOME"] = str(HOST_ROOT)

    COMMAND_HISTORY.append(f"[{CURRENT_PROJECT_NAME}] {working_dir}$ {command}  # process={process_id}")

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
    )

    record = ProcessRecord(
        command=command,
        cwd=working_dir,
        process=process,
        started_at=time.time(),
    )

    with PROCESS_LOCK:
        PROCESSES[process_id] = record

    reader = threading.Thread(
        target=_read_process_output,
        args=(process_id, record),
        daemon=True,
        name=f"process-output-{process_id}",
    )
    reader.start()

    return "\n".join([
        f"process_id: {process_id}",
        f"pid: {process.pid}",
        f"status: {_process_status(record)}",
        f"cwd: {working_dir}",
        f"command: {command}",
    ])


@mcp.tool()
def list_processes() -> str:
    """List tracked background processes."""
    rows = []

    with PROCESS_LOCK:
        items = list(PROCESSES.items())

    for process_id, record in items:
        rows.append(
            "\n".join([
                f"process_id: {process_id}",
                f"pid: {record.process.pid}",
                f"status: {_process_status(record)}",
                f"cwd: {record.cwd}",
                f"started_at_unix: {record.started_at}",
                f"total_output_lines: {record.total_lines}",
                f"command: {record.command}",
            ])
        )

    return "\n\n".join(rows) if rows else "No tracked processes."


@mcp.tool()
def get_process_output(process_id: str, max_lines: int = 300, since_line: Optional[int] = None) -> str:
    """Return captured process output without blocking. Use since_line for incremental reads."""
    with PROCESS_LOCK:
        record = PROCESSES.get(process_id)
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
        selected = buffered_lines[start_index:start_index + max_lines]
        start_line = max(since_line, first_buffered_line)

    header = "\n".join([
        f"process_id: {process_id}",
        f"pid: {record.process.pid}",
        f"status: {_process_status(record)}",
        f"exit_code: {record.process.poll()}",
        f"total_output_lines: {total_lines}",
        f"first_buffered_line: {first_buffered_line}",
        f"returned_start_line: {start_line if selected else ''}",
        f"next_since_line: {start_line + len(selected) if selected else total_lines + 1}",
    ])

    numbered = "\n".join(
        f"{line_no}: {line}"
        for line_no, line in enumerate(selected, start=start_line)
    )

    return f"{header}\n\n{numbered}"[:MAX_OUTPUT]


@mcp.tool()
def wait_for_process_output(process_id: str, text: str, timeout_seconds: int = 30, since_line: Optional[int] = None) -> str:
    """Wait until text appears in background process output, without blocking forever."""
    deadline = time.time() + min(max(timeout_seconds, 1), 300)

    while time.time() < deadline:
        with PROCESS_LOCK:
            record = PROCESSES.get(process_id)
            if record is None:
                raise ValueError("Unknown process_id")

            total_lines = record.total_lines
            buffered_lines = list(record.output)

        first_buffered_line = max(total_lines - len(buffered_lines) + 1, 1)
        start_index = 0 if since_line is None else max(since_line - first_buffered_line, 0)

        for offset, line in enumerate(buffered_lines[start_index:], start=start_index):
            line_no = first_buffered_line + offset
            if text in line:
                return "\n".join([
                    "matched: true",
                    f"process_id: {process_id}",
                    f"line: {line_no}",
                    f"next_since_line: {line_no + 1}",
                    f"text: {line}",
                ])

        if record.process.poll() is not None and record.reader_done:
            break

        time.sleep(0.2)

    return "\n".join([
        "matched: false",
        f"process_id: {process_id}",
        f"status: {_process_status(record)}",
        f"total_output_lines: {record.total_lines}",
        f"next_since_line: {record.total_lines + 1}",
    ])


@mcp.tool()
def stop_process(process_id: str, kill: bool = False) -> str:
    """Stop a tracked process. Uses terminate first unless kill=True."""
    with PROCESS_LOCK:
        record = PROCESSES.get(process_id)

    if record is None:
        raise ValueError("Unknown process_id")

    if record.process.poll() is None:
        if kill:
            record.process.kill()
        else:
            record.process.terminate()

        try:
            record.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            record.process.kill()
            record.process.wait(timeout=5)

    status = _process_status(record)
    return f"Stopped process {process_id}: {status}"


@mcp.tool()
def forget_process(process_id: str) -> str:
    """Remove a stopped process from the tracked process list."""
    with PROCESS_LOCK:
        record = PROCESSES.get(process_id)

        if record is None:
            raise ValueError("Unknown process_id")

        if record.process.poll() is None:
            raise ValueError("Process is still running; stop it before forgetting it")

        del PROCESSES[process_id]

    return f"Forgot process {process_id}"


@mcp.tool()
def apply_patch(patch: str, cwd: str = ".") -> str:
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
    return shell("git status --short", cwd)


@mcp.tool()
def git_diff(cwd: str = ".") -> str:
    return shell("git diff", cwd)


@mcp.tool()
def git_log(cwd: str = ".", max_count: int = 10) -> str:
    return shell(f"git log --oneline -n {max_count}", cwd)


@mcp.tool()
def git_branch(cwd: str = ".") -> str:
    return shell("git branch --show-current && git branch", cwd)


@mcp.tool()
def git_checkout(branch: str, create: bool = False, cwd: str = ".") -> str:
    safe_branch = shlex.quote(branch)
    command = f"git checkout {'-b ' if create else ''}{safe_branch}"
    return shell(command, cwd)


@mcp.tool()
def git_commit(message: str, cwd: str = ".") -> str:
    safe_message = shlex.quote(message)
    return shell(f"git add . && git commit -m {safe_message}", cwd)


@mcp.tool()
def git_restore(path: str = ".", cwd: str = ".") -> str:
    safe_path = shlex.quote(path)
    return shell(f"git restore {safe_path}", cwd)


@mcp.tool()
def fetch_url(url: str, timeout_seconds: int = 10) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "coding-agent-mcp"})

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(150_000).decode("utf-8", errors="replace")
            return f"status: {response.status}\n\n{body}"
    except urllib.error.HTTPError as exc:
        body = exc.read(150_000).decode("utf-8", errors="replace")
        return f"status: {exc.code}\n\n{body}"



def _load_playwright():
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError(
            "Playwright is not installed. Rebuild the Docker image after installing playwright."
        ) from exc
    return async_playwright


def _format_browser_result(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)[:MAX_OUTPUT]


def _normalize_url(url: str) -> str:
    if url.startswith(("http://", "https://", "file://")):
        return url
    return f"http://{url}"



async def _collect_page_summary(page, include_text: bool = True, text_limit: int = 8000) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "url": page.url,
        "title": await page.title(),
    }

    try:
        summary["headings"] = await page.locator("h1,h2,h3").evaluate_all(
            "els => els.slice(0, 40).map(e => ({tag: e.tagName.toLowerCase(), text: (e.innerText || '').trim()})).filter(x => x.text)"
        )
    except Exception as exc:
        summary["headings_error"] = str(exc)

    try:
        summary["links"] = await page.locator("a[href]").evaluate_all(
            "els => els.slice(0, 80).map(e => ({text: (e.innerText || e.getAttribute('aria-label') || '').trim(), href: e.href})).filter(x => x.text || x.href)"
        )
    except Exception as exc:
        summary["links_error"] = str(exc)

    try:
        summary["buttons"] = await page.locator("button, [role=button], input[type=button], input[type=submit]").evaluate_all(
            "els => els.slice(0, 80).map(e => ({text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim(), type: e.getAttribute('type') || '', disabled: !!e.disabled}))"
        )
    except Exception as exc:
        summary["buttons_error"] = str(exc)

    try:
        summary["inputs"] = await page.locator("input, textarea, select").evaluate_all(
            "els => els.slice(0, 80).map(e => ({tag: e.tagName.toLowerCase(), name: e.getAttribute('name') || '', type: e.getAttribute('type') || '', placeholder: e.getAttribute('placeholder') || '', ariaLabel: e.getAttribute('aria-label') || '', value: e.value || ''}))"
        )
    except Exception as exc:
        summary["inputs_error"] = str(exc)

    if include_text:
        try:
            body_text = await page.locator("body").inner_text(timeout=2000)
            summary["text"] = body_text[:text_limit]
            summary["text_truncated"] = len(body_text) > text_limit
        except Exception as exc:
            summary["text_error"] = str(exc)

    return summary


async def _new_browser_page(playwright, width: int = 1280, height: int = 720):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": width, "height": height},
        ignore_https_errors=True,
    )
    page = await context.new_page()
    return browser, context, page





def _safe_int(value: Any, default: int, minimum: int = 1, maximum: int = 100_000) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return min(max(parsed, minimum), maximum)


def _bounded_timeout_ms(timeout_seconds: int) -> int:
    return min(max(timeout_seconds, 1), 120) * 1000


def _append_browser_events(page, console_messages: list[dict[str, Any]], page_errors: list[str], failed_requests: list[dict[str, Any]], responses: list[dict[str, Any]]) -> None:
    page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location}) if msg.type in {"error", "warning"} else None)
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("requestfailed", lambda req: failed_requests.append({"url": req.url, "method": req.method, "failure": str(req.failure) if req.failure else ""}))
    page.on("response", lambda resp: responses.append({"url": resp.url, "status": resp.status, "status_text": resp.status_text}) if resp.status >= 400 else None)


def _attach_session_events(session_id: str, page, record: BrowserSessionRecord) -> None:
    page.on("console", lambda msg: record.console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location}) if msg.type in {"error", "warning"} else None)
    page.on("pageerror", lambda exc: record.page_errors.append(str(exc)))
    page.on("requestfailed", lambda req: record.failed_requests.append({"url": req.url, "method": req.method, "failure": str(req.failure) if req.failure else ""}))
    page.on("response", lambda resp: record.responses.append({"url": resp.url, "status": resp.status, "status_text": resp.status_text}) if resp.status >= 400 else None)


async def _locator_from_action(page, action: dict[str, Any]):
    selector = action.get("selector")
    if selector is not None:
        return page.locator(str(selector))

    role = action.get("role")
    if role is not None:
        name = action.get("name")
        exact = bool(action.get("exact", False))
        if name is None:
            return page.get_by_role(str(role))
        return page.get_by_role(str(role), name=str(name), exact=exact)

    text = action.get("text")
    if text is not None:
        return page.get_by_text(str(text), exact=bool(action.get("exact", False)))

    label = action.get("label")
    if label is not None:
        return page.get_by_label(str(label), exact=bool(action.get("exact", False)))

    placeholder = action.get("placeholder")
    if placeholder is not None:
        return page.get_by_placeholder(str(placeholder), exact=bool(action.get("exact", False)))

    test_id = action.get("test_id")
    if test_id is not None:
        return page.get_by_test_id(str(test_id))

    alt_text = action.get("alt_text")
    if alt_text is not None:
        return page.get_by_alt_text(str(alt_text), exact=bool(action.get("exact", False)))

    title = action.get("title")
    if title is not None:
        return page.get_by_title(str(title), exact=bool(action.get("exact", False)))

    raise ValueError("Action needs one locator field: selector, role/name, text, label, placeholder, test_id, alt_text, or title")


async def _run_browser_action(page, action: dict[str, Any], index: int, timeout_ms: int) -> dict[str, Any]:
    kind = str(action.get("action", "")).strip().lower()
    value = action.get("value", "")
    result: dict[str, Any] = {"index": index, "action": kind}

    if kind in {"click", "dblclick", "fill", "type", "press", "hover", "check", "uncheck", "select", "focus", "blur"}:
        locator = await _locator_from_action(page, action)
        if kind == "click":
            await locator.click(button=str(action.get("button", "left")), click_count=_safe_int(action.get("click_count", 1), 1, 1, 10))
        elif kind == "dblclick":
            await locator.dblclick()
        elif kind == "fill":
            await locator.fill(str(value))
        elif kind == "type":
            await locator.type(str(value), delay=_safe_int(action.get("delay_ms", 0), 0, 0, 1000))
        elif kind == "press":
            await locator.press(str(value))
        elif kind == "hover":
            await locator.hover()
        elif kind == "check":
            await locator.check()
        elif kind == "uncheck":
            await locator.uncheck()
        elif kind == "select":
            await locator.select_option(value)
        elif kind == "focus":
            await locator.focus()
        elif kind == "blur":
            await locator.blur()
    elif kind == "wait_for_selector":
        await page.wait_for_selector(str(action.get("selector")), timeout=timeout_ms, state=str(action.get("state", "visible")))
    elif kind == "wait_for_text":
        locator = page.get_by_text(str(action.get("text", "")), exact=bool(action.get("exact", False)))
        await locator.wait_for(timeout=timeout_ms, state=str(action.get("state", "visible")))
    elif kind == "wait_for_url":
        await page.wait_for_url(str(action.get("url", "**")), timeout=timeout_ms)
    elif kind == "wait":
        await page.wait_for_timeout(_safe_int(action.get("ms", 1000), 1000, 1, timeout_ms))
    elif kind == "goto":
        await page.goto(_normalize_url(str(action.get("url"))), wait_until=str(action.get("wait_until", "load")), timeout=timeout_ms)
    elif kind == "reload":
        await page.reload(wait_until=str(action.get("wait_until", "load")), timeout=timeout_ms)
    elif kind == "assert_text":
        text = str(action.get("text", ""))
        body_text = await page.locator("body").inner_text(timeout=timeout_ms)
        contains = text in body_text
        expected = bool(action.get("expected", True))
        result.update({"text": text, "matched": contains, "expected": expected})
        if contains != expected:
            raise AssertionError(f"assert_text failed: expected {expected}, got {contains} for {text!r}")
    elif kind == "assert_selector":
        locator = await _locator_from_action(page, action)
        count = await locator.count()
        expected = bool(action.get("expected", True))
        matched = count > 0
        result.update({"count": count, "matched": matched, "expected": expected})
        if matched != expected:
            raise AssertionError(f"assert_selector failed: expected {expected}, got {matched}")
    elif kind == "assert_count":
        locator = await _locator_from_action(page, action)
        count = await locator.count()
        expected_count = _safe_int(action.get("count", 0), 0, 0, 1_000_000)
        result.update({"count": count, "expected_count": expected_count})
        if count != expected_count:
            raise AssertionError(f"assert_count failed: expected {expected_count}, got {count}")
    elif kind == "assert_url":
        pattern = str(action.get("url", ""))
        matched = pattern in page.url
        expected = bool(action.get("expected", True))
        result.update({"url_pattern": pattern, "matched": matched, "expected": expected})
        if matched != expected:
            raise AssertionError(f"assert_url failed: expected {expected}, got {matched} for {pattern!r}")
    elif kind == "assert_title":
        expected_title = str(action.get("title", ""))
        title = await page.title()
        matched = expected_title in title
        expected = bool(action.get("expected", True))
        result.update({"title": title, "expected_title": expected_title, "matched": matched, "expected": expected})
        if matched != expected:
            raise AssertionError(f"assert_title failed: expected {expected}, got {matched} for {expected_title!r}")
    elif kind == "evaluate":
        value = await page.evaluate(str(action.get("script", "undefined")))
        result["result"] = value
    else:
        raise ValueError(f"Unsupported browser action: {kind}")

    result["url"] = page.url
    return result


async def _run_browser_actions(page, actions: list[dict[str, Any]], timeout_ms: int) -> list[dict[str, Any]]:
    action_results: list[dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        action_results.append(await _run_browser_action(page, action, index, timeout_ms))
    return action_results


async def _evaluate_dom_snapshot(page, selector: str, element_limit: int, text_limit: int, include_attributes: bool) -> dict[str, Any]:
    return await page.evaluate(
        """
        ({selector, elementLimit, textLimit, includeAttributes}) => {
          const root = document.querySelector(selector) || document.body;
          const importantTags = new Set(['A','BUTTON','INPUT','TEXTAREA','SELECT','OPTION','LABEL','FORM','H1','H2','H3','H4','H5','H6','NAV','MAIN','HEADER','FOOTER','SECTION','ARTICLE','DIALOG','SUMMARY']);
          const importantRoles = new Set(['button','link','textbox','checkbox','radio','combobox','menu','menuitem','tab','tabpanel','dialog','alert','status','navigation','main','banner','contentinfo','form','search']);
          function visible(el) {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width >= 0 && rect.height >= 0;
          }
          function cssPath(el) {
            if (!el || el.nodeType !== Node.ELEMENT_NODE) return '';
            if (el.id) return `#${CSS.escape(el.id)}`;
            const parts = [];
            while (el && el.nodeType === Node.ELEMENT_NODE && el !== document.body) {
              let part = el.tagName.toLowerCase();
              if (el.classList && el.classList.length) part += '.' + [...el.classList].slice(0, 3).map(c => CSS.escape(c)).join('.');
              const parent = el.parentElement;
              if (parent) {
                const siblings = [...parent.children].filter(x => x.tagName === el.tagName);
                if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(el) + 1})`;
              }
              parts.unshift(part);
              el = parent;
            }
            return parts.join(' > ');
          }
          function labelFor(el) {
            if (!el) return '';
            const id = el.getAttribute('id');
            const labels = [];
            if (id) document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(l => labels.push((l.innerText || '').trim()));
            const parentLabel = el.closest('label');
            if (parentLabel) labels.push((parentLabel.innerText || '').trim());
            return labels.filter(Boolean).join(' | ');
          }
          function attrs(el) {
            if (!includeAttributes) return undefined;
            const keep = ['id','class','name','type','role','aria-label','aria-labelledby','placeholder','href','value','alt','title','data-testid','disabled','checked','selected'];
            const out = {};
            for (const name of keep) {
              if (el.hasAttribute(name)) out[name] = el.getAttribute(name);
            }
            return out;
          }
          const nodes = [...root.querySelectorAll('*')];
          const selected = [];
          for (const el of nodes) {
            if (selected.length >= elementLimit) break;
            const role = el.getAttribute('role') || '';
            const text = ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim()).replace(/\\s+/g, ' ');
            const isImportant = importantTags.has(el.tagName) || importantRoles.has(role) || el.hasAttribute('aria-label') || el.hasAttribute('data-testid') || el.onclick;
            if (!isImportant && !text) continue;
            selected.push({
              index: selected.length + 1,
              tag: el.tagName.toLowerCase(),
              role,
              path: cssPath(el),
              text: text.slice(0, textLimit),
              label: labelFor(el).slice(0, textLimit),
              visible: visible(el),
              disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
              attributes: attrs(el),
            });
          }
          return {
            url: location.href,
            title: document.title,
            selector,
            element_count_seen: nodes.length,
            element_count_returned: selected.length,
            elements: selected,
          };
        }
        """,
        {
            "selector": selector,
            "elementLimit": element_limit,
            "textLimit": text_limit,
            "includeAttributes": include_attributes,
        },
    )


async def _evaluate_accessibility_snapshot(page, element_limit: int, text_limit: int) -> dict[str, Any]:
    return await page.evaluate(
        """
        ({elementLimit, textLimit}) => {
          function textOf(el) {
            return ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || '').trim()).replace(/\\s+/g, ' ').slice(0, textLimit);
          }
          function labelFor(el) {
            const id = el.getAttribute('id');
            const parts = [];
            if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label'));
            if (el.getAttribute('aria-labelledby')) {
              for (const ref of el.getAttribute('aria-labelledby').split(/\\s+/)) {
                const node = document.getElementById(ref);
                if (node) parts.push(textOf(node));
              }
            }
            if (id) document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(l => parts.push(textOf(l)));
            const parentLabel = el.closest('label');
            if (parentLabel) parts.push(textOf(parentLabel));
            if (!parts.length) parts.push(textOf(el));
            return [...new Set(parts.filter(Boolean))].join(' | ').slice(0, textLimit);
          }
          function implicitRole(el) {
            const tag = el.tagName.toLowerCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (tag === 'a' && el.href) return 'link';
            if (tag === 'button') return 'button';
            if (tag === 'textarea') return 'textbox';
            if (tag === 'select') return 'combobox';
            if (tag === 'input') {
              if (['button','submit','reset'].includes(type)) return 'button';
              if (type === 'checkbox') return 'checkbox';
              if (type === 'radio') return 'radio';
              if (type === 'range') return 'slider';
              return 'textbox';
            }
            if (/^h[1-6]$/.test(tag)) return 'heading';
            if (tag === 'nav') return 'navigation';
            if (tag === 'main') return 'main';
            if (tag === 'header') return 'banner';
            if (tag === 'footer') return 'contentinfo';
            if (tag === 'form') return 'form';
            if (tag === 'dialog') return 'dialog';
            return '';
          }
          function visible(el) {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width >= 0 && rect.height >= 0;
          }
          function cssPath(el) {
            if (el.id) return `#${CSS.escape(el.id)}`;
            const parts = [];
            while (el && el.nodeType === Node.ELEMENT_NODE && el !== document.body) {
              let part = el.tagName.toLowerCase();
              const parent = el.parentElement;
              if (parent) {
                const siblings = [...parent.children].filter(x => x.tagName === el.tagName);
                if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(el) + 1})`;
              }
              parts.unshift(part);
              el = parent;
            }
            return parts.join(' > ');
          }
          const nodes = [...document.querySelectorAll('a[href],button,input,textarea,select,label,form,h1,h2,h3,h4,h5,h6,nav,main,header,footer,dialog,[role],[aria-label],[aria-labelledby],[tabindex]')];
          const elements = [];
          for (const el of nodes) {
            if (elements.length >= elementLimit) break;
            const role = el.getAttribute('role') || implicitRole(el);
            const name = labelFor(el);
            elements.push({
              index: elements.length + 1,
              role,
              name,
              tag: el.tagName.toLowerCase(),
              path: cssPath(el),
              visible: visible(el),
              disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
              checked: !!el.checked || el.getAttribute('aria-checked') || undefined,
              href: el.href || undefined,
              value: (el.value || '').slice(0, textLimit) || undefined,
            });
          }
          return {url: location.href, title: document.title, element_count_returned: elements.length, elements};
        }
        """,
        {"elementLimit": element_limit, "textLimit": text_limit},
    )


async def _evaluate_form_snapshot(page, element_limit: int, text_limit: int) -> dict[str, Any]:
    return await page.evaluate(
        """
        ({elementLimit, textLimit}) => {
          function labelFor(el) {
            const id = el.getAttribute('id');
            const parts = [];
            if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label'));
            if (id) document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(l => parts.push((l.innerText || '').trim()));
            const parentLabel = el.closest('label');
            if (parentLabel) parts.push((parentLabel.innerText || '').trim());
            return parts.filter(Boolean).join(' | ').slice(0, textLimit);
          }
          const forms = [...document.querySelectorAll('form')].slice(0, elementLimit).map((form, formIndex) => ({
            index: formIndex + 1,
            id: form.id || '',
            name: form.getAttribute('name') || '',
            action: form.action || '',
            method: form.method || '',
            fields: [...form.querySelectorAll('input,textarea,select,button')].slice(0, elementLimit).map((el, index) => ({
              index: index + 1,
              tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type') || '',
              name: el.getAttribute('name') || '',
              id: el.id || '',
              label: labelFor(el),
              placeholder: el.getAttribute('placeholder') || '',
              value: (el.value || '').slice(0, textLimit),
              disabled: !!el.disabled,
              required: !!el.required,
            }))
          }));
          const orphanFields = [...document.querySelectorAll('input,textarea,select,button')].filter(el => !el.closest('form')).slice(0, elementLimit).map((el, index) => ({
            index: index + 1,
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            id: el.id || '',
            label: labelFor(el),
            placeholder: el.getAttribute('placeholder') || '',
            value: (el.value || '').slice(0, textLimit),
            disabled: !!el.disabled,
            required: !!el.required,
          }));
          return {url: location.href, title: document.title, forms, orphan_fields: orphanFields};
        }
        """,
        {"elementLimit": element_limit, "textLimit": text_limit},
    )


def _get_browser_session(session_id: str) -> BrowserSessionRecord:
    record = BROWSER_SESSIONS.get(session_id)
    if record is None:
        raise ValueError(f"Unknown browser session: {session_id}")
    return record


@mcp.tool()
async def browser_inspect(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    text_limit: int = 8000,
) -> str:
    """Inspect a page: title, final URL, headings, links, buttons, inputs, and visible body text."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = min(max(timeout_seconds, 1), 120) * 1000

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            return _format_browser_result(await _collect_page_summary(page, include_text=True, text_limit=text_limit))
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_check_errors(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
) -> str:
    """Open a page and collect console errors, page exceptions, failed requests, and non-2xx/3xx responses."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = min(max(timeout_seconds, 1), 120) * 1000
    console_messages: list[dict[str, str]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, str]] = []
    bad_responses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text}) if msg.type in {"error", "warning"} else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("requestfailed", lambda req: failed_requests.append({"url": req.url, "failure": str(req.failure) if req.failure else ""}))
        page.on("response", lambda resp: bad_responses.append({"url": resp.url, "status": resp.status}) if resp.status >= 400 else None)

        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            await page.wait_for_timeout(1000)
            result = await _collect_page_summary(page, include_text=False)
            result.update({
                "console_messages": console_messages[:100],
                "page_errors": page_errors[:100],
                "failed_requests": failed_requests[:100],
                "bad_responses": bad_responses[:100],
                "ok": not console_messages and not page_errors and not failed_requests and not bad_responses,
            })
            return _format_browser_result(result)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_interact(
    url: str,
    actions: list[dict[str, Any]],
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
    text_limit: int = 8000,
) -> str:
    """Run browser actions from a fresh page and return a text DOM summary. Locators can use selector, role/name, text, label, placeholder, test_id, alt_text, or title. Supported actions: click, dblclick, fill, type, press, hover, check, uncheck, select, focus, blur, wait_for_selector, wait_for_text, wait_for_url, wait, goto, reload, assert_text, assert_selector, assert_count, assert_url, assert_title, evaluate."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    bad_responses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.set_default_timeout(timeout_ms)
        _append_browser_events(page, console_messages, page_errors, failed_requests, bad_responses)

        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            action_results = await _run_browser_actions(page, actions, timeout_ms)
            result = await _collect_page_summary(page, include_text=True, text_limit=text_limit)
            result.update({
                "actions": action_results,
                "console_messages": console_messages[:100],
                "page_errors": page_errors[:100],
                "failed_requests": failed_requests[:100],
                "bad_responses": bad_responses[:100],
                "ok": not console_messages and not page_errors and not failed_requests and not bad_responses,
            })
            return _format_browser_result(result)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_evaluate(
    url: str,
    script: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
) -> str:
    """Evaluate JavaScript on a page and return the JSON-serializable result."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = min(max(timeout_seconds, 1), 120) * 1000

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            value = await page.evaluate(script)
            return _format_browser_result({
                "url": page.url,
                "title": await page.title(),
                "result": value,
            })
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_dom_snapshot(
    url: str,
    selector: str = "body",
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    element_limit: int = 250,
    text_limit: int = 500,
    include_attributes: bool = True,
) -> str:
    """Return a structured, text-only DOM snapshot focused on useful/interactive elements. No screenshots or images."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            data = await _evaluate_dom_snapshot(page, selector, _safe_int(element_limit, 250, 1, 2000), _safe_int(text_limit, 500, 1, 5000), include_attributes)
            return _format_browser_result(data)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_accessibility_snapshot(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    element_limit: int = 250,
    text_limit: int = 500,
) -> str:
    """Return a text-only accessibility-oriented snapshot: roles, names, fields, headings, links, landmarks, and controls."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            return _format_browser_result(await _evaluate_accessibility_snapshot(page, _safe_int(element_limit, 250, 1, 2000), _safe_int(text_limit, 500, 1, 5000)))
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_form_snapshot(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    element_limit: int = 200,
    text_limit: int = 500,
) -> str:
    """Return form/action/field metadata for a page, including labels, names, placeholders, values, required and disabled flags."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            return _format_browser_result(await _evaluate_form_snapshot(page, _safe_int(element_limit, 200, 1, 2000), _safe_int(text_limit, 500, 1, 5000)))
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_assert(
    url: str,
    assertions: list[dict[str, Any]],
    actions: Optional[list[dict[str, Any]]] = None,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
) -> str:
    """Run optional actions and assertion actions against a fresh page. Assertion actions include assert_text, assert_selector, assert_count, assert_url, and assert_title."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    bad_responses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.set_default_timeout(timeout_ms)
        _append_browser_events(page, console_messages, page_errors, failed_requests, bad_responses)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            action_results = await _run_browser_actions(page, actions or [], timeout_ms)
            assertion_results = await _run_browser_actions(page, assertions, timeout_ms)
            return _format_browser_result({
                "url": page.url,
                "title": await page.title(),
                "actions": action_results,
                "assertions": assertion_results,
                "console_messages": console_messages[:100],
                "page_errors": page_errors[:100],
                "failed_requests": failed_requests[:100],
                "bad_responses": bad_responses[:100],
                "ok": not console_messages and not page_errors and not failed_requests and not bad_responses,
            })
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_network_trace(
    url: str,
    actions: Optional[list[dict[str, Any]]] = None,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
    max_events: int = 200,
) -> str:
    """Open a page, optionally run actions, and return request/response/failure/console traces as text JSON."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    event_limit = _safe_int(max_events, 200, 1, 2000)
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.set_default_timeout(timeout_ms)
        page.on("request", lambda req: requests.append({"url": req.url, "method": req.method, "resource_type": req.resource_type}) if len(requests) < event_limit else None)
        page.on("response", lambda resp: responses.append({"url": resp.url, "status": resp.status, "status_text": resp.status_text}) if len(responses) < event_limit else None)
        page.on("requestfailed", lambda req: failed_requests.append({"url": req.url, "method": req.method, "failure": str(req.failure) if req.failure else ""}) if len(failed_requests) < event_limit else None)
        page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location}) if len(console_messages) < event_limit else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)) if len(page_errors) < event_limit else None)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            action_results = await _run_browser_actions(page, actions or [], timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
            except Exception:
                pass
            return _format_browser_result({
                "url": page.url,
                "title": await page.title(),
                "actions": action_results,
                "requests": requests[:event_limit],
                "responses": responses[:event_limit],
                "failed_requests": failed_requests[:event_limit],
                "console_messages": console_messages[:event_limit],
                "page_errors": page_errors[:event_limit],
                "bad_responses": [r for r in responses if r.get("status", 0) >= 400][:event_limit],
                "ok": not failed_requests and not page_errors and not [r for r in responses if r.get("status", 0) >= 400] and not [m for m in console_messages if m.get("type") in {"error", "warning"}],
            })
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_storage_state(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    include_local_storage: bool = True,
    include_session_storage: bool = True,
    include_cookies: bool = True,
) -> str:
    """Return cookies, localStorage, and sessionStorage for a page after navigation."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            result: dict[str, Any] = {"url": page.url, "title": await page.title()}
            if include_cookies:
                result["cookies"] = await context.cookies()
            if include_local_storage:
                result["local_storage"] = await page.evaluate("Object.fromEntries(Object.entries(localStorage))")
            if include_session_storage:
                result["session_storage"] = await page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
            return _format_browser_result(result)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_session_open(
    url: str,
    session_id: Optional[str] = None,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
    text_limit: int = 8000,
) -> str:
    """Open a persistent browser session for multi-step testing across MCP calls. Close it with browser_session_close."""
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    session_id = (session_id or str(uuid.uuid4())[:8]).strip()
    if not session_id:
        session_id = str(uuid.uuid4())[:8]
    if session_id in BROWSER_SESSIONS:
        raise ValueError(f"Browser session already exists: {session_id}")

    playwright = await async_playwright().start()
    browser, context, page = await _new_browser_page(playwright, width, height)
    page.set_default_timeout(timeout_ms)
    record = BrowserSessionRecord(playwright=playwright, browser=browser, context=context, page=page, created_at=time.time())
    _attach_session_events(session_id, page, record)
    BROWSER_SESSIONS[session_id] = record
    try:
        await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
        summary = await _collect_page_summary(page, include_text=True, text_limit=text_limit)
        summary.update({"session_id": session_id, "created_at_unix": record.created_at})
        return _format_browser_result(summary)
    except Exception:
        await context.close()
        await browser.close()
        await playwright.stop()
        BROWSER_SESSIONS.pop(session_id, None)
        raise


@mcp.tool()
def browser_session_list() -> str:
    """List open persistent browser sessions."""
    sessions = []
    for session_id, record in BROWSER_SESSIONS.items():
        sessions.append({
            "session_id": session_id,
            "url": record.page.url,
            "created_at_unix": record.created_at,
            "console_messages": len(record.console_messages),
            "page_errors": len(record.page_errors),
            "failed_requests": len(record.failed_requests),
            "bad_responses": len(record.responses),
        })
    return _format_browser_result({"sessions": sessions})


@mcp.tool()
async def browser_session_close(session_id: str) -> str:
    """Close a persistent browser session and free its browser process."""
    record = _get_browser_session(session_id)
    BROWSER_SESSIONS.pop(session_id, None)
    await record.context.close()
    await record.browser.close()
    await record.playwright.stop()
    return f"Closed browser session: {session_id}"


@mcp.tool()
async def browser_session_inspect(session_id: str, text_limit: int = 8000) -> str:
    """Inspect the current page in a persistent browser session."""
    record = _get_browser_session(session_id)
    result = await _collect_page_summary(record.page, include_text=True, text_limit=text_limit)
    result.update({"session_id": session_id})
    return _format_browser_result(result)


@mcp.tool()
async def browser_session_interact(
    session_id: str,
    actions: list[dict[str, Any]],
    timeout_seconds: int = 20,
    text_limit: int = 8000,
) -> str:
    """Run actions in an existing persistent browser session and return the updated text DOM summary."""
    record = _get_browser_session(session_id)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    record.page.set_default_timeout(timeout_ms)
    action_results = await _run_browser_actions(record.page, actions, timeout_ms)
    result = await _collect_page_summary(record.page, include_text=True, text_limit=text_limit)
    result.update({
        "session_id": session_id,
        "actions": action_results,
        "console_messages": list(record.console_messages)[-100:],
        "page_errors": list(record.page_errors)[-100:],
        "failed_requests": list(record.failed_requests)[-100:],
        "bad_responses": list(record.responses)[-100:],
    })
    return _format_browser_result(result)


@mcp.tool()
async def browser_session_evaluate(session_id: str, script: str) -> str:
    """Evaluate JavaScript in an existing persistent browser session."""
    record = _get_browser_session(session_id)
    value = await record.page.evaluate(script)
    return _format_browser_result({"session_id": session_id, "url": record.page.url, "title": await record.page.title(), "result": value})


@mcp.tool()
async def browser_session_dom_snapshot(
    session_id: str,
    selector: str = "body",
    element_limit: int = 250,
    text_limit: int = 500,
    include_attributes: bool = True,
) -> str:
    """Return a structured DOM snapshot for the current page in a persistent browser session."""
    record = _get_browser_session(session_id)
    data = await _evaluate_dom_snapshot(record.page, selector, _safe_int(element_limit, 250, 1, 2000), _safe_int(text_limit, 500, 1, 5000), include_attributes)
    data["session_id"] = session_id
    return _format_browser_result(data)


@mcp.tool()
async def browser_session_accessibility_snapshot(
    session_id: str,
    element_limit: int = 250,
    text_limit: int = 500,
) -> str:
    """Return an accessibility-oriented snapshot for the current page in a persistent browser session."""
    record = _get_browser_session(session_id)
    data = await _evaluate_accessibility_snapshot(record.page, _safe_int(element_limit, 250, 1, 2000), _safe_int(text_limit, 500, 1, 5000))
    data["session_id"] = session_id
    return _format_browser_result(data)


@mcp.tool()
def browser_session_logs(session_id: str, max_entries: int = 100) -> str:
    """Return recent console errors/warnings, page errors, failed requests, and bad responses for a persistent browser session."""
    record = _get_browser_session(session_id)
    limit = _safe_int(max_entries, 100, 1, BROWSER_LOG_LIMIT)
    return _format_browser_result({
        "session_id": session_id,
        "url": record.page.url,
        "console_messages": list(record.console_messages)[-limit:],
        "page_errors": list(record.page_errors)[-limit:],
        "failed_requests": list(record.failed_requests)[-limit:],
        "bad_responses": list(record.responses)[-limit:],
        "ok": not record.console_messages and not record.page_errors and not record.failed_requests and not record.responses,
    })


@mcp.tool()
def create_snapshot(name: Optional[str] = None) -> str:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)

    snapshot_id = name or f"{CURRENT_PROJECT_NAME}-{int(time.time())}"
    target = SNAPSHOT_ROOT / snapshot_id

    ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".venv", "dist", "build")
    shutil.copytree(CURRENT_PROJECT, target, ignore=ignore)

    return f"Created snapshot: {snapshot_id}"


@mcp.tool()
def restore_snapshot(snapshot_id: str) -> str:
    source = (SNAPSHOT_ROOT / snapshot_id).resolve()

    if not source.exists() or SNAPSHOT_ROOT not in source.parents:
        raise FileNotFoundError("Snapshot not found")

    for item in CURRENT_PROJECT.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for item in source.iterdir():
        dest = CURRENT_PROJECT / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    return f"Restored snapshot: {snapshot_id}"


@mcp.tool()
def list_snapshots() -> str:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    return "\n".join(sorted(path.name for path in SNAPSHOT_ROOT.iterdir() if path.is_dir()))


@mcp.tool()
def get_project_info() -> str:
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
        f"name: {CURRENT_PROJECT_NAME}",
        f"path: {CURRENT_PROJECT}",
    ]

    for key, marker in markers.items():
        lines.append(f"{key}: {(CURRENT_PROJECT / marker).exists()}")

    return "\n".join(lines)


@mcp.tool()
def environment_info() -> str:
    commands = [
        "pwd",
        "python --version || true",
        "node --version || true",
        "npm --version || true",
        "git --version || true",
        "docker --version || true",
    ]

    return shell(" && ".join(commands))


@mcp.tool()
def install_apt_packages(packages: list[str]) -> str:
    """Install Debian packages inside the MCP container."""
    safe_packages = " ".join(shlex.quote(pkg) for pkg in packages)

    return shell(
        f"apt-get update && apt-get install -y --no-install-recommends {safe_packages}",
        cwd="/",
        timeout_seconds=300,
    )


@mcp.tool()
def install_python_packages(packages: list[str]) -> str:
    """Install Python packages globally inside the MCP container."""
    safe_packages = " ".join(shlex.quote(pkg) for pkg in packages)

    return shell(
        f"pip install {safe_packages}",
        cwd=".",
        timeout_seconds=300,
    )


@mcp.tool()
def install_node_packages(packages: list[str], dev: bool = False, package_manager: str = "npm") -> str:
    """Install Node packages in the active project."""
    safe_packages = " ".join(shlex.quote(pkg) for pkg in packages)

    if package_manager == "npm":
        command = f"npm install {'--save-dev ' if dev else ''}{safe_packages}"
    elif package_manager == "pnpm":
        command = f"pnpm add {'-D ' if dev else ''}{safe_packages}"
    elif package_manager == "yarn":
        command = f"yarn add {'-D ' if dev else ''}{safe_packages}"
    else:
        raise ValueError("package_manager must be npm, pnpm, or yarn")

    return shell(command, timeout_seconds=300)


@mcp.tool()
def install_project_dependencies(package_manager: Optional[str] = None) -> str:
    """Install dependencies for the active project."""
    if package_manager is None:
        if (CURRENT_PROJECT / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (CURRENT_PROJECT / "yarn.lock").exists():
            package_manager = "yarn"
        elif (CURRENT_PROJECT / "package-lock.json").exists() or (CURRENT_PROJECT / "package.json").exists():
            package_manager = "npm"
        elif (CURRENT_PROJECT / "requirements.txt").exists():
            return shell("pip install -r requirements.txt", timeout_seconds=300)
        elif (CURRENT_PROJECT / "pyproject.toml").exists():
            return shell("pip install -e .", timeout_seconds=300)
        else:
            raise ValueError("Could not detect project dependency manager")

    if package_manager == "npm":
        return shell("npm install", timeout_seconds=300)

    if package_manager == "pnpm":
        return shell("pnpm install", timeout_seconds=300)

    if package_manager == "yarn":
        return shell("yarn install", timeout_seconds=300)

    if package_manager == "pip":
        return shell("pip install -r requirements.txt", timeout_seconds=300)

    raise ValueError("Unsupported package manager")

if __name__ == "__main__":
    PROJECTS["host"] = HOST_ROOT
    mcp.run(transport="streamable-http")