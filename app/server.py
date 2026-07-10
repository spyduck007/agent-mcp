import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
import hashlib
import base64
import difflib
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections.abc import Iterable
from collections import deque
from dataclasses import dataclass, field
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

try:
    import jwt
except ImportError:  # pragma: no cover - clearer startup error below
    jwt = None


AUTH_MODE = os.getenv("AUTH_MODE", "required").lower()
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspaces")).resolve()
WORKSPACE_MAP_PATH = Path(os.getenv("WORKSPACE_MAP_PATH", "/config/workspaces.json"))
SECRET_REFS_PATH = Path(os.getenv("SECRET_REFS_PATH", "/config/secrets.json"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "agent-mcp")
OIDC_ISSUER = os.getenv("OIDC_ISSUER", f"{PUBLIC_URL.rstrip('/')}/auth/realms/{KEYCLOAK_REALM}").rstrip("/")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "chatgpt-agent-mcp")
OIDC_JWKS_URL = os.getenv("OIDC_JWKS_URL", f"{OIDC_ISSUER}/protocol/openid-connect/certs")


class OidcTokenVerifier(TokenVerifier):
    """Validate JWT access tokens issued by the configured OAuth/OIDC provider."""

    def __init__(self, issuer: str, audience: str, client_id: str, jwks_url: str) -> None:
        if jwt is None:
            raise RuntimeError("PyJWT is required when AUTH_MODE=required")
        self.issuer = issuer
        self.audience = audience
        self.client_id = client_id
        self.jwks = jwt.PyJWKClient(jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token).key
            decode_options: dict[str, Any] = {"require": ["exp", "iss", "sub"]}
            if not self.audience:
                decode_options["verify_aud"] = False
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "ES256", "PS256"],
                audience=self.audience or None,
                issuer=self.issuer,
                options=decode_options,
            )
        except Exception:
            return None

        raw_scopes = claims.get("scope", claims.get("scp", []))
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes or [])
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        if self.client_id and claims.get("azp") != self.client_id:
            return None
        realm_roles = claims.get("realm_access", {}).get("roles", [])
        if isinstance(realm_roles, list):
            scopes.extend(str(role) for role in realm_roles)
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp", claims.get("client_id", "unknown"))),
            scopes=[str(scope) for scope in scopes],
            expires_at=int(claims["exp"]),
            subject=subject,
            claims=claims,
        )


def _auth_settings() -> tuple[AuthSettings | None, TokenVerifier | None]:
    if AUTH_MODE == "disabled":
        return None, None
    if AUTH_MODE != "required":
        raise RuntimeError("AUTH_MODE must be 'required' or 'disabled'")
    missing = [name for name, value in {
        "OIDC_ISSUER": OIDC_ISSUER,
        "OIDC_CLIENT_ID": OIDC_CLIENT_ID,
        "OIDC_JWKS_URL": OIDC_JWKS_URL,
        "PUBLIC_URL": PUBLIC_URL,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Refusing to start without OAuth configuration: {', '.join(missing)}")
    return (
        AuthSettings(issuer_url=OIDC_ISSUER, resource_server_url=PUBLIC_URL, required_scopes=[]),
        OidcTokenVerifier(OIDC_ISSUER, OIDC_AUDIENCE, OIDC_CLIENT_ID, OIDC_JWKS_URL),
    )


AUTH_SETTINGS, TOKEN_VERIFIER = _auth_settings()

mcp = FastMCP(
    "coding-agent-mcp",
    instructions=(
        "Use read tools to inspect before changing files. Keep all paths inside the assigned workspace. "
        "Use write tools only after the user has requested a change; never use destructive actions or commands "
        "unless the user explicitly asks. Run a relevant verification command after edits."
    ),
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8080")),
    streamable_http_path="/mcp",
    auth=AUTH_SETTINGS,
    token_verifier=TOKEN_VERIFIER,
)

SNAPSHOT_ROOT = Path(os.getenv("SNAPSHOT_ROOT", "/snapshots")).resolve()
AUDIT_ROOT = Path(os.getenv("AUDIT_ROOT", "/snapshots/audit")).resolve()
MEMORY_ROOT = Path(os.getenv("MEMORY_ROOT", "/snapshots/memory")).resolve()
AGENT_POLICY_PATH = Path(os.getenv("AGENT_POLICY_PATH", "/config/agent-policy.md"))
SELF_IMPROVEMENT_WORKSPACE = os.getenv("SELF_IMPROVEMENT_WORKSPACE", "agent-mcp")

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


@dataclass
class UserSessionState:
    subject: str
    projects: dict[str, Path]
    current_project_name: str
    current_project: Path
    processes: dict[str, ProcessRecord] = field(default_factory=dict)
    browser_sessions: dict[str, BrowserSessionRecord] = field(default_factory=dict)
    command_history: list[str] = field(default_factory=list)


SESSIONS: dict[str, UserSessionState] = {}
SESSION_LOCK = threading.RLock()
PROCESS_LOCK = threading.Lock()

MAX_READ_BYTES = 500_000
MAX_OUTPUT = 80_000
DEFAULT_TIMEOUT_SECONDS = 30


def _workspace_map() -> dict[str, list[Path]]:
    """Load the subject-to-workspace allowlist. Paths must remain under WORKSPACE_ROOT."""
    if not WORKSPACE_MAP_PATH.exists():
        if AUTH_MODE == "disabled":
            return {"local-dev": [WORKSPACE_ROOT]}
        raise PermissionError(f"Workspace map is missing: {WORKSPACE_MAP_PATH}")
    raw = json.loads(WORKSPACE_MAP_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Workspace map must be a JSON object")
    result: dict[str, list[Path]] = {}
    for subject, roots in raw.items():
        values = [roots] if isinstance(roots, str) else roots
        if not isinstance(subject, str) or not isinstance(values, list):
            raise ValueError("Workspace map values must be paths or lists of paths")
        resolved = [Path(value).resolve() for value in values]
        if not all(root.is_relative_to(WORKSPACE_ROOT) for root in resolved):
            raise ValueError("Every workspace must be beneath WORKSPACE_ROOT")
        result[subject] = resolved
    return result


def _identity() -> tuple[str, set[str]]:
    token = get_access_token()
    if token is None:
        if AUTH_MODE == "disabled":
            return "local-dev", {"workspace:read", "workspace:write", "command:run", "browser:use", "network:fetch", "admin:install", "secrets:use", "database:use", "deploy:run", "github:write"}
        raise PermissionError("OAuth authentication is required")
    if not token.subject:
        raise PermissionError("OAuth token has no subject")
    return token.subject, set(token.scopes)


def require_scope(scope: str) -> None:
    _subject, scopes = _identity()
    if scope not in scopes:
        raise PermissionError(f"OAuth scope required: {scope}")


def session_state() -> UserSessionState:
    subject, _scopes = _identity()
    with SESSION_LOCK:
        existing = SESSIONS.get(subject)
        if existing is not None:
            return existing
        roots = _workspace_map().get(subject, [])
        if not roots:
            raise PermissionError("No workspace has been assigned to this identity")
        root = roots[0]
        state = UserSessionState(subject=subject, projects={"workspace": root}, current_project_name="workspace", current_project=root)
        SESSIONS[subject] = state
        return state


def _is_allowed_path(target: Path, roots: Iterable[Path]) -> bool:
    return any(target.is_relative_to(root) for root in roots)


def user_snapshot_root() -> Path:
    """Keep snapshot names private to the authenticated identity."""
    subject = session_state().subject.encode("utf-8")
    root = SNAPSHOT_ROOT / hashlib.sha256(subject).hexdigest()[:24]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _identity_storage_root(base: Path) -> Path:
    """Return a per-identity directory outside project worktrees."""
    subject = session_state().subject.encode("utf-8")
    root = base / hashlib.sha256(subject).hexdigest()[:24]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _json_result(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def _audit(event: str, details: dict[str, Any]) -> None:
    """Write a bounded, credential-free audit record for high-level agent actions."""
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "subject": session_state().subject,
            "project": session_state().current_project_name,
            "event": event,
            "details": details,
        }
        target = _identity_storage_root(AUDIT_ROOT) / "events.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    except Exception:
        # An audit-volume failure must never turn a safe read operation into an outage.
        pass


def set_current_project(name: str, path: Path) -> None:
    state = session_state()
    state.projects[name] = path.resolve()
    state.current_project_name = name
    state.current_project = path.resolve()


def resolve_path(path: str = ".") -> Path:
    require_scope("workspace:read")
    state = session_state()
    raw = Path(path).expanduser()

    if raw.is_absolute():
        target = raw.resolve()
    else:
        target = (state.current_project / raw).resolve()

    roots = _workspace_map().get(state.subject, [])
    if not _is_allowed_path(target, roots):
        raise PermissionError("Path is outside the assigned workspace")

    return target


def shell(command: str, cwd: str = ".", timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    require_scope("command:run")
    working_dir = resolve_path(cwd)
    timeout = min(max(timeout_seconds, 1), 300)

    env = os.environ.copy()
    env["HOME"] = str(session_state().current_project)

    state = session_state()
    state.command_history.append(f"[{state.current_project_name}] {working_dir}$ {command}")

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
    state = session_state()
    if name not in state.projects:
        raise ValueError(f"Unknown project: {name}")

    set_current_project(name, state.projects[name])
    return f"Switched to project '{name}' at {state.current_project}"


@mcp.tool(annotations={"readOnlyHint": True})
def list_projects() -> str:
    """List opened projects."""
    state = session_state()
    if not state.projects:
        return "No projects opened."

    return "\n".join(
        f"{'*' if name == state.current_project_name else ' '} {name}: {path}"
        for name, path in state.projects.items()
    )


@mcp.tool()
def close_project(name: str) -> str:
    """Forget an opened project."""
    state = session_state()

    if name not in state.projects:
        raise ValueError(f"Unknown project: {name}")

    del state.projects[name]

    if name == state.current_project_name:
        if not state.projects:
            raise ValueError("Cannot close the last workspace project")
        state.current_project_name, state.current_project = next(iter(state.projects.items()))

    return f"Closed project: {name}"


@mcp.tool(annotations={"readOnlyHint": True})
def pwd() -> str:
    """Return the active project directory."""
    state = session_state()
    return f"{state.current_project_name}: {state.current_project}"


@mcp.tool()
def write_file(file_path: str, contents: str) -> str:
    require_scope("workspace:write")
    target = resolve_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    return f"Wrote {len(contents)} characters to {target}"


@mcp.tool()
def append_file(file_path: str, contents: str) -> str:
    require_scope("workspace:write")
    target = resolve_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("a", encoding="utf-8") as f:
        f.write(contents)

    return f"Appended {len(contents)} characters to {target}"


@mcp.tool(annotations={"readOnlyHint": True})
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


@mcp.tool(annotations={"readOnlyHint": True})
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
    require_scope("workspace:write")
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
    require_scope("workspace:write")
    target = resolve_path(file_path)
    lines = target.read_text(encoding="utf-8").splitlines()

    before = lines[:start_line - 1]
    after = lines[end_line:]
    replacement = new_content.splitlines()

    target.write_text("\n".join(before + replacement + after) + "\n", encoding="utf-8")
    return f"Replaced lines {start_line}-{end_line} in {target}"


@mcp.tool()
def insert_at_line(file_path: str, line_number: int, content: str) -> str:
    require_scope("workspace:write")
    target = resolve_path(file_path)
    lines = target.read_text(encoding="utf-8").splitlines()

    index = max(min(line_number - 1, len(lines)), 0)
    lines[index:index] = content.splitlines()

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"Inserted content at line {line_number} in {target}"


@mcp.tool()
def copy_path(source: str, destination: str) -> str:
    require_scope("workspace:write")
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
    require_scope("workspace:write")
    src = resolve_path(source)
    dst = resolve_path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} to {dst}"


@mcp.tool()
def delete_path(path: str) -> str:
    require_scope("workspace:write")
    target = resolve_path(path)

    if target.is_dir():
        shutil.rmtree(target)
        return f"Deleted directory: {target}"

    target.unlink()
    return f"Deleted file: {target}"


@mcp.tool()
def create_directory(directory: str) -> str:
    require_scope("workspace:write")
    target = resolve_path(directory)
    target.mkdir(parents=True, exist_ok=True)
    return f"Created directory: {target}"


@mcp.tool(annotations={"readOnlyHint": True})
def list_files(directory: str = ".", recursive: bool = True, max_entries: int = 800) -> str:
    root = resolve_path(directory)
    iterator = root.rglob("*") if recursive else root.iterdir()

    entries = []
    for path in iterator:
        if len(entries) >= max_entries:
            entries.append("... output truncated ...")
            break

        try:
            rel = path.relative_to(session_state().current_project)
        except ValueError:
            rel = path

        entries.append(f"{rel}{'/' if path.is_dir() else ''}")

    return "\n".join(entries)


@mcp.tool(annotations={"readOnlyHint": True})
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


@mcp.tool(annotations={"readOnlyHint": True})
def find_file(pattern: str, directory: str = ".", max_results: int = 200) -> str:
    root = resolve_path(directory)
    results = []

    for path in root.rglob(pattern):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break
        results.append(str(path))

    return "\n".join(results)


@mcp.tool(annotations={"readOnlyHint": True})
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


@mcp.tool(annotations={"readOnlyHint": True})
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


@mcp.tool(annotations={"readOnlyHint": True})
def find_symbol(name: str, directory: str = ".", max_results: int = 100) -> str:
    pattern = rf"^\s*(def|class|function|const|let|var|async function|export function|export class)\s+{re.escape(name)}\b"
    return regex_search(pattern, directory, max_results)


@mcp.tool(annotations={"readOnlyHint": True})
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
    return "\n".join(session_state().command_history[-max_entries:])


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
        items = list(session_state().processes.items())

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
    with PROCESS_LOCK:
        record = session_state().processes.get(process_id)

        if record is None:
            raise ValueError("Unknown process_id")

        if record.process.poll() is None:
            raise ValueError("Process is still running; stop it before forgetting it")

        del session_state().processes[process_id]

    return f"Forgot process {process_id}"


@mcp.tool()
def apply_patch(patch: str, cwd: str = ".") -> str:
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
    require_scope("network:fetch")
    request = urllib.request.Request(url, headers={"User-Agent": "coding-agent-mcp"})

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(150_000).decode("utf-8", errors="replace")
            return f"status: {response.status}\n\n{body}"
    except urllib.error.HTTPError as exc:
        body = exc.read(150_000).decode("utf-8", errors="replace")
        return f"status: {exc.code}\n\n{body}"



def _load_playwright():
    require_scope("browser:use")
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
    record = session_state().browser_sessions.get(session_id)
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
    state = session_state()
    if session_id in state.browser_sessions:
        raise ValueError(f"Browser session already exists: {session_id}")

    playwright = await async_playwright().start()
    browser, context, page = await _new_browser_page(playwright, width, height)
    await context.tracing.start(screenshots=False, snapshots=True, sources=True)
    page.set_default_timeout(timeout_ms)
    record = BrowserSessionRecord(playwright=playwright, browser=browser, context=context, page=page, created_at=time.time())
    _attach_session_events(session_id, page, record)
    state.browser_sessions[session_id] = record
    try:
        await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
        summary = await _collect_page_summary(page, include_text=True, text_limit=text_limit)
        summary.update({"session_id": session_id, "created_at_unix": record.created_at})
        return _format_browser_result(summary)
    except Exception:
        await context.close()
        await browser.close()
        await playwright.stop()
        state.browser_sessions.pop(session_id, None)
        raise


@mcp.tool()
def browser_session_list() -> str:
    """List open persistent browser sessions."""
    require_scope("browser:use")
    sessions = []
    for session_id, record in session_state().browser_sessions.items():
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
    session_state().browser_sessions.pop(session_id, None)
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
    require_scope("browser:use")
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
def list_snapshots() -> str:
    require_scope("workspace:read")
    return "\n".join(sorted(path.name for path in user_snapshot_root().iterdir() if path.is_dir()))


def _run_argv(argv: list[str], cwd: Path, timeout_seconds: int = 300) -> dict[str, Any]:
    """Run a fixed argv command and return bounded structured output."""
    try:
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout_seconds)
        return {"argv": argv, "exit_code": result.returncode, "timed_out": False, "stdout": result.stdout[:MAX_OUTPUT], "stderr": result.stderr[:MAX_OUTPUT]}
    except subprocess.TimeoutExpired as exc:
        return {"argv": argv, "exit_code": None, "timed_out": True, "stdout": (exc.stdout or "")[:MAX_OUTPUT], "stderr": (exc.stderr or "")[:MAX_OUTPUT]}


def _project_memory_file() -> Path:
    state = session_state()
    digest = hashlib.sha256(str(state.current_project).encode("utf-8")).hexdigest()[:24]
    return _identity_storage_root(MEMORY_ROOT) / f"{digest}.json"


def _load_project_memory() -> dict[str, Any]:
    target = _project_memory_file()
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_project_memory(memory: dict[str, Any]) -> None:
    target = _project_memory_file()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(memory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def _project_markers(root: Path) -> dict[str, bool]:
    return {
        "git": (root / ".git").exists(),
        "python": (root / "pyproject.toml").exists() or (root / "requirements.txt").exists(),
        "node": (root / "package.json").exists(),
        "docker": (root / "Dockerfile").exists(),
        "compose": (root / "docker-compose.yml").exists() or (root / "compose.yml").exists(),
        "tests": (root / "tests").exists() or (root / "test").exists(),
        "readme": (root / "README.md").exists(),
    }


def _verification_commands(root: Path) -> dict[str, list[list[str]]]:
    commands: dict[str, list[list[str]]] = {"syntax": [], "test": [], "lint": [], "typecheck": [], "build": [], "compose": []}
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        commands["syntax"].append(["python", "-m", "compileall", "-q", "."])
    if (root / "tests").exists() or (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
        commands["test"].append(["python", "-m", "pytest", "-q"])
    if (root / "package.json").exists():
        for suite in ("test", "lint", "typecheck", "build"):
            commands[suite].append(["npm", "run", "--if-present", suite])
    if (root / ".mcp-compose-validation.env").exists() and ((root / "docker-compose.yml").exists() or (root / "compose.yml").exists()):
        commands["compose"].append(["docker", "compose", "--env-file", ".mcp-compose-validation.env", "config", "--quiet"])
    return commands


@mcp.tool(annotations={"readOnlyHint": True})
def project_context(max_files: int = 120) -> str:
    """Return grounded project context: detected stack, root files, Git state, available verification suites, stored notes, and the active safety policy."""
    require_scope("workspace:read")
    root = session_state().current_project
    markers = _project_markers(root)
    root_files = sorted(path.name for path in root.iterdir())[:min(max(max_files, 1), 500)]
    git = _run_argv(["git", "status", "--short", "--branch"], root, 30) if markers["git"] else None
    package_scripts: list[str] = []
    if (root / "package.json").exists():
        try:
            package_scripts = sorted(json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {}).keys())
        except (OSError, json.JSONDecodeError, AttributeError):
            package_scripts = []
    policy = AGENT_POLICY_PATH.read_text(encoding="utf-8")[:12_000] if AGENT_POLICY_PATH.exists() else "No policy file configured."
    memory = _load_project_memory()
    return _json_result({
        "project": session_state().current_project_name,
        "path": str(root),
        "markers": markers,
        "root_files": root_files,
        "git": git,
        "package_scripts": package_scripts,
        "verification_suites": {name: commands for name, commands in _verification_commands(root).items() if commands},
        "memory_keys": sorted(memory),
        "policy": policy,
    })


@mcp.tool()
def project_memory_set(key: str, value: str) -> str:
    """Persist a concise, user-approved project fact or decision outside the repository. Never store credentials or tokens here."""
    require_scope("workspace:write")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", key):
        raise ValueError("Memory key may contain only letters, digits, '.', '_' and '-'")
    if len(value) > 12_000:
        raise ValueError("Memory values are limited to 12,000 characters")
    memory = _load_project_memory()
    memory[key] = {"updated_at": datetime.now(timezone.utc).isoformat(), "value": value}
    _save_project_memory(memory)
    _audit("project_memory_set", {"key": key, "characters": len(value)})
    return _json_result({"saved": key, "memory_keys": sorted(memory)})


@mcp.tool(annotations={"readOnlyHint": True})
def project_memory_get(key: Optional[str] = None) -> str:
    """Read one persistent project memory entry, or list available memory keys."""
    require_scope("workspace:read")
    memory = _load_project_memory()
    if key is None:
        return _json_result({"memory_keys": sorted(memory)})
    if key not in memory:
        raise KeyError(f"No memory entry named {key}")
    return _json_result({"key": key, "entry": memory[key]})


@mcp.tool()
def project_checkpoint(summary: str, next_steps: list[str], verification: Optional[dict[str, Any]] = None) -> str:
    """Create a durable progress checkpoint outside the repository for long-running agent work."""
    require_scope("workspace:write")
    if len(summary) > 12_000 or len(next_steps) > 50:
        raise ValueError("Checkpoint is too large")
    root = _identity_storage_root(MEMORY_ROOT) / "checkpoints"
    checkpoint_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    payload = {"id": checkpoint_id, "at": datetime.now(timezone.utc).isoformat(), "project": str(session_state().current_project), "summary": summary, "next_steps": next_steps, "verification": verification or {}}
    (root / f"{checkpoint_id}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _audit("project_checkpoint", {"checkpoint_id": checkpoint_id, "next_step_count": len(next_steps)})
    return _json_result(payload)


@mcp.tool()
def project_verify(suites: Optional[list[str]] = None, timeout_seconds: int = 300) -> str:
    """Run detected fixed verification suites (syntax, test, lint, typecheck, build, compose) and return grounded pass/fail evidence."""
    require_scope("command:run")
    root = session_state().current_project
    available = _verification_commands(root)
    selected = suites or [name for name, commands in available.items() if commands]
    allowed = set(available)
    if unknown := set(selected) - allowed:
        raise ValueError(f"Unknown verification suite(s): {', '.join(sorted(unknown))}")
    results: dict[str, list[dict[str, Any]]] = {}
    timeout = min(max(timeout_seconds, 1), 600)
    for suite in selected:
        results[suite] = [_run_argv(command, root, timeout) for command in available[suite]]
    passed = all(item["exit_code"] == 0 and not item["timed_out"] for items in results.values() for item in items)
    payload = {"project": str(root), "selected_suites": selected, "passed": passed, "results": results}
    _audit("project_verify", {"suites": selected, "passed": passed})
    return _json_result(payload)


@mcp.tool(annotations={"readOnlyHint": True})
def self_improvement_readiness() -> str:
    """Assess whether the active project is the isolated self-improvement checkout and report the branch, policy, verification, and credential prerequisites."""
    require_scope("workspace:read")
    root = session_state().current_project
    is_self_workspace = root.name == SELF_IMPROVEMENT_WORKSPACE
    markers = _project_markers(root)
    git = _run_argv(["git", "status", "--short", "--branch"], root, 30) if markers["git"] else None
    origin = _run_argv(["git", "remote", "get-url", "origin"], root, 30) if markers["git"] else None
    return _json_result({
        "ready": is_self_workspace and markers["git"] and AGENT_POLICY_PATH.exists(),
        "is_isolated_self_workspace": is_self_workspace,
        "expected_workspace_name": SELF_IMPROVEMENT_WORKSPACE,
        "git": git,
        "origin": origin,
        "verification_suites": [name for name, commands in _verification_commands(root).items() if commands],
        "github_pr_requirement": "Configure a fine-grained GitHub token as GITHUB_TOKEN in /config/secrets.json, then use github_push_branch and github_create_pull_request.",
        "deployment_requirement": "Use deployment_preflight and require explicit user approval before deployment_apply or deployment_rollback.",
        "policy_present": AGENT_POLICY_PATH.exists(),
    })


@mcp.tool(annotations={"readOnlyHint": True})
def audit_events(max_entries: int = 100) -> str:
    """Return recent credential-free audit records for the authenticated identity."""
    require_scope("workspace:read")
    target = _identity_storage_root(AUDIT_ROOT) / "events.jsonl"
    if not target.exists():
        return _json_result({"events": []})
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-min(max(max_entries, 1), 1000):]
    return _json_result({"events": [json.loads(line) for line in lines if line.strip()]})


@mcp.tool()
def deployment_preflight(cwd: str = ".") -> str:
    """Validate a Compose project before deployment: configuration, current service state, and a snapshot identifier for rollback."""
    require_scope("deploy:run")
    require_scope("workspace:write")
    root = resolve_path(cwd)
    if not ((root / "docker-compose.yml").exists() or (root / "compose.yml").exists()):
        raise FileNotFoundError("No docker-compose.yml or compose.yml in the selected project")
    config = _run_argv(["docker", "compose", "config", "--quiet"], root, 120)
    status = _run_argv(["docker", "compose", "ps", "--all"], root, 60)
    snapshot = create_snapshot(f"predeploy-{int(time.time())}")
    payload = {"project": str(root), "config": config, "status": status, "snapshot": snapshot, "ready": config["exit_code"] == 0}
    _audit("deployment_preflight", {"project": str(root), "ready": payload["ready"]})
    return _json_result(payload)


@mcp.tool()
def deployment_apply(approval: str, services: Optional[list[str]] = None, health_url: Optional[str] = None, cwd: str = ".") -> str:
    """Build and apply a Compose deployment. Requires the exact user approval phrase I_APPROVE_DEPLOYMENT and optional HTTP health evidence."""
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
    payload = {"deployed": result["exit_code"] == 0 and (health is None or health.get("healthy") is True), "preflight": preflight, "apply": result, "health": health}
    _audit("deployment_apply", {"project": str(root), "services": service_args, "deployed": payload["deployed"]})
    return _json_result(payload)


@mcp.tool()
def deployment_rollback(snapshot_id: str, approval: str, cwd: str = ".") -> str:
    """Restore a pre-deployment snapshot and re-apply Compose. Requires exact user approval I_APPROVE_ROLLBACK."""
    require_scope("deploy:run")
    require_scope("workspace:write")
    if approval != "I_APPROVE_ROLLBACK":
        raise PermissionError("Rollback requires explicit user approval: I_APPROVE_ROLLBACK")
    restore = restore_snapshot(snapshot_id)
    root = resolve_path(cwd)
    result = _run_argv(["docker", "compose", "up", "--build", "--detach"], root, 600)
    payload = {"restored": restore, "apply": result, "rolled_back": result["exit_code"] == 0}
    _audit("deployment_rollback", {"project": str(root), "snapshot_id": snapshot_id, "rolled_back": payload["rolled_back"]})
    return _json_result(payload)


@mcp.tool()
def github_push_branch(branch: str, secret_ref: str = "GITHUB_TOKEN", cwd: str = ".") -> str:
    """Push a branch using an ephemeral fine-grained GitHub token reference; the token is never persisted in Git config or returned."""
    require_scope("github:write")
    require_scope("workspace:write")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,180}", branch):
        raise ValueError("Invalid branch name")
    token = _secret_values([secret_ref])[secret_ref]
    root = resolve_path(cwd)
    auth = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    result = _run_argv(["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: Basic {auth}", "push", "--set-upstream", "origin", branch], root, 300)
    safe_result = dict(result)
    safe_result["argv"] = ["git", "push", "--set-upstream", "origin", branch]
    _audit("github_push_branch", {"branch": branch, "exit_code": result["exit_code"]})
    return _json_result({"branch": branch, "result": safe_result, "secret_ref_used": secret_ref})


@mcp.tool()
def github_create_pull_request(repository: str, head: str, title: str, body: str, base: str = "main", secret_ref: str = "GITHUB_TOKEN") -> str:
    """Create a GitHub pull request with an ephemeral fine-grained token. Requires repository format owner/name and github:write."""
    require_scope("github:write")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    token = _secret_values([secret_ref])[secret_ref]
    payload = json.dumps({"title": title[:256], "head": head, "base": base, "body": body[:60_000]}).encode("utf-8")
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/pulls", data=payload, method="POST", headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
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


@mcp.tool()
def get_project_info() -> str:
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
    require_scope("admin:install")
    safe_packages = " ".join(shlex.quote(pkg) for pkg in packages)

    return shell(
        f"apt-get update && apt-get install -y --no-install-recommends {safe_packages}",
        cwd=".",
        timeout_seconds=300,
    )


@mcp.tool()
def install_python_packages(packages: list[str]) -> str:
    """Install Python packages globally inside the MCP container."""
    require_scope("admin:install")
    safe_packages = " ".join(shlex.quote(pkg) for pkg in packages)

    return shell(
        f"pip install {safe_packages}",
        cwd=".",
        timeout_seconds=300,
    )


@mcp.tool()
def install_node_packages(packages: list[str], dev: bool = False, package_manager: str = "npm") -> str:
    """Install Node packages in the active project."""
    require_scope("workspace:write")
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
    require_scope("workspace:write")
    if package_manager is None:
        project = session_state().current_project
        if (project / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (project / "yarn.lock").exists():
            package_manager = "yarn"
        elif (project / "package-lock.json").exists() or (project / "package.json").exists():
            package_manager = "npm"
        elif (project / "requirements.txt").exists():
            return shell("pip install -r requirements.txt", timeout_seconds=300)
        elif (project / "pyproject.toml").exists():
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


# Advanced low-level primitives. These keep the broad agent capability available
# while providing structured inputs and outputs for reliable higher-level flows.

def _secret_values(names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    require_scope("secrets:use")
    if not SECRET_REFS_PATH.exists():
        raise FileNotFoundError("Secret reference file is not configured")
    values = json.loads(SECRET_REFS_PATH.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("Secret reference file must contain a JSON object")
    missing = [name for name in names if not isinstance(values.get(name), str)]
    if missing:
        raise ValueError(f"Unknown secret reference(s): {', '.join(missing)}")
    return {name: values[name] for name in names}


def _command_environment(environment: Optional[dict[str, str]], secret_refs: Optional[list[str]]) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(session_state().current_project)
    for name, value in (environment or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid environment variable name: {name}")
        env[name] = str(value)
    env.update(_secret_values(secret_refs or []))
    return env


@mcp.tool()
def run_command_advanced(
    argv: list[str],
    cwd: str = ".",
    environment: Optional[dict[str, str]] = None,
    secret_refs: Optional[list[str]] = None,
    stdin: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_file: Optional[str] = None,
) -> str:
    """Run an argv command without shell parsing. Supports controlled environment values, named secret injection, stdin, timeout, and optional captured-output file. Secret values are never returned by this tool."""
    require_scope("command:run")
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise ValueError("argv must contain one or more non-empty strings")
    working_dir = resolve_path(cwd)
    timeout = min(max(timeout_seconds, 1), 300)
    state = session_state()
    state.command_history.append(f"[{state.current_project_name}] {working_dir}$ {shlex.join(argv)}")
    try:
        result = subprocess.run(
            argv,
            cwd=working_dir,
            env=_command_environment(environment, secret_refs),
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        result = None
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    else:
        stdout = result.stdout
        stderr = result.stderr

    combined = f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
    output_path = None
    if output_file:
        require_scope("workspace:write")
        target = resolve_path(output_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(combined, encoding="utf-8")
        output_path = str(target)
    return _format_browser_result({
        "argv": argv,
        "cwd": str(working_dir),
        "exit_code": None if timed_out else result.returncode,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "stdout": stdout[:MAX_OUTPUT],
        "stderr": stderr[:MAX_OUTPUT],
        "output_file": output_path,
        "secret_refs_used": secret_refs or [],
    })


@mcp.tool()
def file_hash(file_path: str, algorithm: str = "sha256") -> str:
    """Return a cryptographic hash of a workspace file without returning its contents."""
    if algorithm not in hashlib.algorithms_available:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")
    target = resolve_path(file_path)
    digest = hashlib.new(algorithm)
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return _format_browser_result({"path": str(target), "algorithm": algorithm, "digest": digest.hexdigest(), "size_bytes": target.stat().st_size})


@mcp.tool()
def read_binary_file(file_path: str, max_bytes: int = 500_000) -> str:
    """Read a binary workspace file as bounded base64 data."""
    target = resolve_path(file_path)
    limit = min(max(max_bytes, 1), MAX_READ_BYTES)
    data = target.read_bytes()[:limit]
    return _format_browser_result({"path": str(target), "data_base64": base64.b64encode(data).decode("ascii"), "size_bytes_returned": len(data), "truncated": target.stat().st_size > len(data)})


@mcp.tool()
def write_binary_file(file_path: str, data_base64: str, expected_sha256: Optional[str] = None) -> str:
    """Write base64 binary data atomically within the assigned workspace."""
    require_scope("workspace:write")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise ValueError("data_base64 is not valid base64") from exc
    target = resolve_path(file_path)
    if expected_sha256 and target.exists() and file_hash(file_path).find(expected_sha256) < 0:
        raise ValueError("Existing file hash does not match expected_sha256")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return _format_browser_result({"path": str(target), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})


@mcp.tool()
def atomic_write_file(file_path: str, contents: str, expected_sha256: Optional[str] = None) -> str:
    """Atomically replace a text file, optionally only when its current SHA-256 matches."""
    require_scope("workspace:write")
    target = resolve_path(file_path)
    if expected_sha256 and target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current != expected_sha256:
            raise ValueError("Existing file hash does not match expected_sha256")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(target)
    return _format_browser_result({"path": str(target), "sha256": hashlib.sha256(contents.encode()).hexdigest(), "characters": len(contents)})


@mcp.tool()
def diff_files(left_path: str, right_path: str, context_lines: int = 3) -> str:
    """Return a unified diff between two workspace text files."""
    left = resolve_path(left_path)
    right = resolve_path(right_path)
    context = min(max(context_lines, 0), 100)
    diff = difflib.unified_diff(
        left.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
        right.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
        fromfile=str(left), tofile=str(right), n=context,
    )
    return "".join(diff)[:MAX_OUTPUT]


@mcp.tool()
def search_all_matches(query: str, directory: str = ".", max_results: int = 500, case_sensitive: bool = True) -> str:
    """Search every matching line across workspace text files, rather than only the first match per file."""
    root = resolve_path(directory)
    needle = query if case_sensitive else query.lower()
    rows: list[str] = []
    for path in root.rglob("*"):
        if len(rows) >= max_results:
            rows.append("... output truncated ...")
            break
        if not path.is_file() or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    rows.append(f"{path}:{line_number}: {line}")
                    if len(rows) >= max_results:
                        break
        except OSError:
            continue
    return "\n".join(rows)


@mcp.tool()
def chmod_path(path: str, mode: str) -> str:
    """Set workspace file permissions using an octal mode such as '755'."""
    require_scope("workspace:write")
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        raise ValueError("mode must be a 3- or 4-digit octal string")
    target = resolve_path(path)
    target.chmod(int(mode, 8))
    return f"Set {target} mode to {mode}"


@mcp.tool()
def create_symlink(target: str, link_path: str) -> str:
    """Create a workspace-contained symbolic link. Both target and link must remain in the assigned workspace."""
    require_scope("workspace:write")
    source = resolve_path(target)
    link = resolve_path(link_path)
    if link.exists() or link.is_symlink():
        raise FileExistsError(link_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(source)
    return f"Created symlink {link} -> {source}"


@mcp.tool(annotations={"readOnlyHint": True})
def read_symlink(path: str) -> str:
    """Return a symbolic link target after validating that it remains in the workspace."""
    link = resolve_path(path)
    if not link.is_symlink():
        raise ValueError("Path is not a symbolic link")
    resolved = link.resolve()
    resolve_path(str(resolved))
    return _format_browser_result({"path": str(link), "link_target": str(link.readlink()), "resolved_target": str(resolved)})


@mcp.tool()
def git_staged_diff(cwd: str = ".") -> str:
    return shell("git diff --cached", cwd)


@mcp.tool()
def git_show(revision: str = "HEAD", cwd: str = ".") -> str:
    return shell(f"git show --stat --patch {shlex.quote(revision)}", cwd)


@mcp.tool()
def git_blame(file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None, cwd: str = ".") -> str:
    line_range = "" if start_line is None else f" -L {max(start_line, 1)},{max(end_line or start_line, start_line)}"
    return shell(f"git blame{line_range} -- {shlex.quote(file_path)}", cwd)


@mcp.tool()
def git_fetch(remote: str = "origin", cwd: str = ".") -> str:
    return shell(f"git fetch {shlex.quote(remote)} --prune", cwd)


@mcp.tool()
def git_pull(remote: str = "origin", branch: Optional[str] = None, rebase: bool = True, cwd: str = ".") -> str:
    require_scope("workspace:write")
    branch_part = f" {shlex.quote(branch)}" if branch else ""
    return shell(f"git pull {'--rebase ' if rebase else ''}{shlex.quote(remote)}{branch_part}", cwd)


@mcp.tool()
def git_push(remote: str = "origin", branch: Optional[str] = None, set_upstream: bool = False, cwd: str = ".") -> str:
    require_scope("workspace:write")
    branch_part = f" {shlex.quote(branch)}" if branch else ""
    return shell(f"git push {'-u ' if set_upstream else ''}{shlex.quote(remote)}{branch_part}", cwd)


@mcp.tool()
def git_stash(action: str = "push", message: Optional[str] = None, cwd: str = ".") -> str:
    require_scope("workspace:write")
    if action not in {"push", "pop", "list", "drop"}:
        raise ValueError("action must be push, pop, list, or drop")
    extra = f" -m {shlex.quote(message)}" if action == "push" and message else ""
    return shell(f"git stash {action}{extra}", cwd)


@mcp.tool()
def git_worktree(action: str, path: Optional[str] = None, branch: Optional[str] = None, cwd: str = ".") -> str:
    """List, add, or remove Git worktrees. Added paths must be in the assigned workspace."""
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


@mcp.tool()
def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    body: Optional[str] = None,
    timeout_seconds: int = 20,
) -> str:
    """Make an HTTP request with method, headers, and optional text/JSON body. Response headers, status, timing, and bounded body are returned."""
    require_scope("network:fetch")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    started = time.monotonic()
    request = urllib.request.Request(url, method=method.upper(), headers=headers or {}, data=body.encode("utf-8") if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=min(max(timeout_seconds, 1), 120)) as response:
            payload = response.read(150_000)
            return _format_browser_result({"url": response.url, "status": response.status, "headers": dict(response.headers.items()), "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "body": payload.decode("utf-8", errors="replace"), "truncated": response.length is not None and response.length > len(payload)})
    except urllib.error.HTTPError as exc:
        return _format_browser_result({"url": url, "status": exc.code, "headers": dict(exc.headers.items()), "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "body": exc.read(150_000).decode("utf-8", errors="replace")})


@mcp.tool()
def http_download(url: str, destination: str, timeout_seconds: int = 60, max_bytes: int = 20_000_000) -> str:
    """Download a bounded HTTP response into the workspace and return its hash."""
    require_scope("network:fetch")
    require_scope("workspace:write")
    target = resolve_path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=min(max(timeout_seconds, 1), 120)) as response:
        data = response.read(min(max(max_bytes, 1), 100_000_000) + 1)
        if len(data) > max_bytes:
            raise ValueError("Download exceeded max_bytes")
        target.write_bytes(data)
        return _format_browser_result({"url": response.url, "status": response.status, "path": str(target), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})


@mcp.tool()
def http_upload(url: str, source_file: str, method: str = "PUT", headers: Optional[dict[str, str]] = None, timeout_seconds: int = 60) -> str:
    """Upload a workspace file as a raw HTTP request body."""
    require_scope("network:fetch")
    source = resolve_path(source_file)
    request = urllib.request.Request(url, method=method.upper(), headers=headers or {}, data=source.read_bytes())
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=min(max(timeout_seconds, 1), 120)) as response:
            return _format_browser_result({"url": response.url, "status": response.status, "headers": dict(response.headers.items()), "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "body": response.read(150_000).decode("utf-8", errors="replace")})
    except urllib.error.HTTPError as exc:
        return _format_browser_result({"url": url, "status": exc.code, "headers": dict(exc.headers.items()), "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "body": exc.read(150_000).decode("utf-8", errors="replace")})


@mcp.tool(annotations={"readOnlyHint": True})
def dns_lookup(hostname: str) -> str:
    """Resolve a hostname to IP addresses."""
    require_scope("network:fetch")
    results = sorted({entry[4][0] for entry in socket.getaddrinfo(hostname, None)})
    return _format_browser_result({"hostname": hostname, "addresses": results})


@mcp.tool(annotations={"readOnlyHint": True})
def tcp_check(hostname: str, port: int, timeout_seconds: int = 5) -> str:
    """Check TCP connectivity and report latency."""
    require_scope("network:fetch")
    started = time.monotonic()
    try:
        with socket.create_connection((hostname, port), timeout=min(max(timeout_seconds, 1), 30)):
            return _format_browser_result({"hostname": hostname, "port": port, "reachable": True, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
    except OSError as exc:
        return _format_browser_result({"hostname": hostname, "port": port, "reachable": False, "error": str(exc), "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})


@mcp.tool(annotations={"readOnlyHint": True})
def tls_certificate(hostname: str, port: int = 443, timeout_seconds: int = 10) -> str:
    """Inspect a TLS certificate and protocol for a host."""
    require_scope("network:fetch")
    context = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=min(max(timeout_seconds, 1), 30)) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as secure:
            return _format_browser_result({"hostname": hostname, "port": port, "protocol": secure.version(), "cipher": secure.cipher(), "certificate": secure.getpeercert()})


@mcp.tool()
async def browser_session_upload(session_id: str, selector: str, files: list[str]) -> str:
    """Upload one or more workspace files through a file input in a persistent browser session."""
    require_scope("workspace:read")
    record = _get_browser_session(session_id)
    paths = [str(resolve_path(path)) for path in files]
    await record.page.locator(selector).set_input_files(paths)
    return _format_browser_result({"session_id": session_id, "selector": selector, "files": paths, "url": record.page.url})


@mcp.tool()
async def browser_session_frame_evaluate(session_id: str, frame_selector: str, script: str) -> str:
    """Evaluate JavaScript inside a selected iframe in a persistent browser session."""
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    frame = record.page.frame_locator(frame_selector)
    result = await frame.locator("html").evaluate(script)
    return _format_browser_result({"session_id": session_id, "frame_selector": frame_selector, "result": result})


@mcp.tool()
async def browser_session_route(session_id: str, url_pattern: str, action: str = "abort", fulfill_body: Optional[str] = None, status: int = 200) -> str:
    """Persistently intercept browser-session requests: abort them or fulfill them with a controlled text response."""
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    if action not in {"abort", "fulfill"}:
        raise ValueError("action must be abort or fulfill")

    async def handler(route):
        if action == "abort":
            await route.abort()
        else:
            await route.fulfill(status=status, body=fulfill_body or "", content_type="text/plain")

    await record.context.route(url_pattern, handler)
    return _format_browser_result({"session_id": session_id, "url_pattern": url_pattern, "action": action})


@mcp.tool()
async def browser_session_trace(session_id: str, destination: str) -> str:
    """Export a Playwright trace ZIP for the persistent session into the workspace, then start a new trace segment."""
    require_scope("workspace:write")
    record = _get_browser_session(session_id)
    target = resolve_path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    await record.context.tracing.stop(path=str(target))
    await record.context.tracing.start(screenshots=False, snapshots=True, sources=True)
    return _format_browser_result({"session_id": session_id, "path": str(target), "size_bytes": target.stat().st_size})


@mcp.tool()
async def browser_session_import_storage(session_id: str, local_storage: Optional[dict[str, str]] = None, session_storage: Optional[dict[str, str]] = None) -> str:
    """Set localStorage and sessionStorage values in the current persistent browser-session page."""
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    await record.page.evaluate("""({localStorageValues, sessionStorageValues}) => {
        for (const [key, value] of Object.entries(localStorageValues || {})) localStorage.setItem(key, value);
        for (const [key, value] of Object.entries(sessionStorageValues || {})) sessionStorage.setItem(key, value);
    }""", {"localStorageValues": local_storage or {}, "sessionStorageValues": session_storage or {}})
    return _format_browser_result({"session_id": session_id, "local_storage_keys": sorted((local_storage or {}).keys()), "session_storage_keys": sorted((session_storage or {}).keys())})


@mcp.tool()
def database_query(engine: str, query: str, database: Optional[str] = None, secret_ref: Optional[str] = None, timeout_seconds: int = 30) -> str:
    """Run a SQLite query against a workspace database or a PostgreSQL query using a named secret connection URL. Use only for trusted development databases."""
    require_scope("database:use")
    timeout = min(max(timeout_seconds, 1), 120)
    if engine == "sqlite":
        if not database:
            raise ValueError("SQLite requires a workspace database path")
        path = resolve_path(database)
        result = subprocess.run(["sqlite3", "-json", str(path), query], text=True, capture_output=True, timeout=timeout)
    elif engine == "postgres":
        if not secret_ref:
            raise ValueError("Postgres requires a secret_ref containing its connection URL")
        connection = _secret_values([secret_ref])[secret_ref]
        result = subprocess.run(["psql", connection, "--no-psqlrc", "--tuples-only", "--no-align", "-c", query], text=True, capture_output=True, timeout=timeout)
    else:
        raise ValueError("engine must be sqlite or postgres")
    return _format_browser_result({"engine": engine, "exit_code": result.returncode, "stdout": result.stdout[:MAX_OUTPUT], "stderr": result.stderr[:MAX_OUTPUT]})


@mcp.tool(annotations={"readOnlyHint": True})
def compose_status(cwd: str = ".") -> str:
    """Return Docker Compose service state for a project."""
    require_scope("deploy:run")
    return shell("docker compose ps --all", cwd)


@mcp.tool(annotations={"readOnlyHint": True})
def compose_logs(service: Optional[str] = None, tail: int = 200, cwd: str = ".") -> str:
    """Return bounded recent Docker Compose logs."""
    require_scope("deploy:run")
    suffix = f" {shlex.quote(service)}" if service else ""
    return shell(f"docker compose logs --tail {min(max(tail, 1), 2000)} --no-color{suffix}", cwd)


@mcp.tool()
def compose_restart(services: Optional[list[str]] = None, cwd: str = ".") -> str:
    """Restart all or selected Docker Compose services."""
    require_scope("deploy:run")
    require_scope("workspace:write")
    suffix = " ".join(shlex.quote(service) for service in services or [])
    return shell(f"docker compose restart {suffix}".rstrip(), cwd)


@mcp.tool(annotations={"readOnlyHint": True})
def wait_for_http_health(url: str, expected_status: int = 200, timeout_seconds: int = 60) -> str:
    """Poll an HTTP endpoint until it returns the expected status or times out."""
    require_scope("deploy:run")
    deadline = time.monotonic() + min(max(timeout_seconds, 1), 300)
    last_status: Optional[int] = None
    last_error: Optional[str] = None
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
    return _format_browser_result({"healthy": False, "url": url, "expected_status": expected_status, "last_status": last_status, "last_error": last_error})


@mcp.tool()
def start_process_advanced(
    argv: list[str],
    cwd: str = ".",
    name: Optional[str] = None,
    environment: Optional[dict[str, str]] = None,
    secret_refs: Optional[list[str]] = None,
    allow_stdin: bool = False,
) -> str:
    """Start an argv background process without shell parsing, with controlled environment, optional named secrets, and optional stdin forwarding."""
    require_scope("command:run")
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise ValueError("argv must contain one or more non-empty strings")
    state = session_state()
    process_id = (name or str(uuid.uuid4())[:8]).strip() or str(uuid.uuid4())[:8]
    if process_id in state.processes:
        raise ValueError(f"Process id already exists: {process_id}")
    working_dir = resolve_path(cwd)
    process = subprocess.Popen(
        argv,
        cwd=working_dir,
        env=_command_environment(environment, secret_refs),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if allow_stdin else subprocess.DEVNULL,
        bufsize=1,
        start_new_session=True,
    )
    record = ProcessRecord(command=shlex.join(argv), cwd=working_dir, process=process, started_at=time.time())
    with PROCESS_LOCK:
        state.processes[process_id] = record
    threading.Thread(target=_read_process_output, args=(process_id, record), daemon=True, name=f"process-output-{process_id}").start()
    state.command_history.append(f"[{state.current_project_name}] {working_dir}$ {shlex.join(argv)}  # process={process_id}")
    return _format_browser_result({"process_id": process_id, "pid": process.pid, "cwd": str(working_dir), "argv": argv, "stdin_enabled": allow_stdin, "secret_refs_used": secret_refs or []})


@mcp.tool()
def send_process_input(process_id: str, data: str, append_newline: bool = True) -> str:
    """Send text to a background process started with allow_stdin=true."""
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
    return _format_browser_result({"process_id": process_id, "characters_sent": len(data), "newline_appended": append_newline})


@mcp.tool()
def signal_process(process_id: str, signal_name: str = "TERM") -> str:
    """Send a POSIX signal (TERM, INT, HUP, KILL, USR1, USR2) to a tracked process group."""
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
    return _format_browser_result({"process_id": process_id, "signal": signal_name.upper(), "status": _process_status(record)})


@mcp.tool(annotations={"readOnlyHint": True})
def port_owner(port: int) -> str:
    """Report listeners bound to a local TCP or UDP port."""
    require_scope("command:run")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return shell(f"ss -lptun 'sport = :{port}' || true", cwd=".")


@mcp.tool()
async def browser_session_download(session_id: str, action: dict[str, Any], destination: str) -> str:
    """Perform a browser action that triggers a download and save the exact downloaded file in the workspace."""
    require_scope("workspace:write")
    record = _get_browser_session(session_id)
    target = resolve_path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    async with record.page.expect_download() as download_info:
        await _run_browser_action(record.page, action, 1, _bounded_timeout_ms(20))
    download = await download_info.value
    await download.save_as(str(target))
    data = target.read_bytes()
    return _format_browser_result({"session_id": session_id, "path": str(target), "suggested_filename": download.suggested_filename, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})


@mcp.tool()
async def browser_session_popup(session_id: str, action: dict[str, Any], timeout_seconds: int = 20) -> str:
    """Perform an action expected to open a popup and make that popup the active page of the persistent session."""
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    async with record.page.expect_popup(timeout=timeout_ms) as popup_info:
        await _run_browser_action(record.page, action, 1, timeout_ms)
    popup = await popup_info.value
    await popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    record.page = popup
    _attach_session_events(session_id, popup, record)
    summary = await _collect_page_summary(popup, include_text=True)
    summary.update({"session_id": session_id, "popup": True})
    return _format_browser_result(summary)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
