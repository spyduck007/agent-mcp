# ruff: noqa: F401
import asyncio
import base64
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse

try:
    import jwt
except ImportError:  # pragma: no cover - clearer startup error below
    jwt = None


READ_ONLY_ANNOTATIONS = ToolAnnotations(readOnlyHint=True)


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 0)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative integer") from exc


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
        jwt_module = jwt
        if jwt_module is None:
            raise RuntimeError("PyJWT is required when AUTH_MODE=required")
        self.jwt = jwt_module
        self.issuer = issuer
        self.audience = audience
        self.client_id = client_id
        self.jwks = jwt_module.PyJWKClient(jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token).key
            decode_options: Any = {"require": ["exp", "iss", "sub"]}
            if not self.audience:
                decode_options["verify_aud"] = False
            claims = self.jwt.decode(
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
    missing = [
        name
        for name, value in {
            "OIDC_ISSUER": OIDC_ISSUER,
            "OIDC_CLIENT_ID": OIDC_CLIENT_ID,
            "OIDC_JWKS_URL": OIDC_JWKS_URL,
            "PUBLIC_URL": PUBLIC_URL,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Refusing to start without OAuth configuration: {', '.join(missing)}")
    return (
        AuthSettings(
            issuer_url=AnyHttpUrl(OIDC_ISSUER),
            resource_server_url=AnyHttpUrl(PUBLIC_URL),
            required_scopes=[],
        ),
        OidcTokenVerifier(OIDC_ISSUER, OIDC_AUDIENCE, OIDC_CLIENT_ID, OIDC_JWKS_URL),
    )


AUTH_SETTINGS, TOKEN_VERIFIER = _auth_settings()

RESOURCE_CLEANUP_LAST_RUN: float | None = None
RESOURCE_CLEANUP_LAST_ERROR: str | None = None


@asynccontextmanager
async def _mcp_lifespan(_server):
    cleanup_task = asyncio.create_task(_resource_cleanup_loop(), name="resource-cleanup")
    try:
        yield {}
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await _cleanup_all_resources(force=True)


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
    lifespan=_mcp_lifespan,
)

SNAPSHOT_ROOT = Path(os.getenv("SNAPSHOT_ROOT", "/snapshots")).resolve()
AUDIT_ROOT = Path(os.getenv("AUDIT_ROOT", "/snapshots/audit")).resolve()
MEMORY_ROOT = Path(os.getenv("MEMORY_ROOT", "/snapshots/memory")).resolve()
AGENT_POLICY_PATH = Path(os.getenv("AGENT_POLICY_PATH", "/config/agent-policy.md"))
SELF_IMPROVEMENT_WORKSPACE = os.getenv("SELF_IMPROVEMENT_WORKSPACE", "agent-mcp")
MAX_PROCESSES_PER_USER = _env_int("MAX_PROCESSES_PER_USER", 32)
MAX_BROWSER_SESSIONS_PER_USER = _env_int("MAX_BROWSER_SESSIONS_PER_USER", 12)
PROCESS_IDLE_TTL_SECONDS = _env_int("PROCESS_IDLE_TTL_SECONDS", 14400)
BROWSER_IDLE_TTL_SECONDS = _env_int("BROWSER_IDLE_TTL_SECONDS", 7200)
FINISHED_PROCESS_RETENTION_SECONDS = _env_int("FINISHED_PROCESS_RETENTION_SECONDS", 3600)
RESOURCE_CLEANUP_INTERVAL_SECONDS = _env_int("RESOURCE_CLEANUP_INTERVAL_SECONDS", 60)

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
    redactions: tuple[str, ...] = ()
    last_activity_at: float = field(default_factory=time.time)
    exited_at: float | None = None


@dataclass
class BrowserSessionRecord:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    created_at: float
    last_activity_at: float = field(default_factory=time.time)
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
            return "local-dev", {
                "workspace:read",
                "workspace:write",
                "command:run",
                "browser:use",
                "network:fetch",
                "admin:install",
                "secrets:use",
                "database:use",
                "deploy:run",
                "github:write",
            }
        raise PermissionError("OAuth authentication is required")
    if not token.subject:
        raise PermissionError("OAuth token has no subject")
    return token.subject, set(token.scopes)


def require_scope(scope: str) -> None:
    _subject, scopes = _identity()
    if scope not in scopes:
        raise PermissionError(f"OAuth scope required: {scope}")


TOOL_SCOPE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "append_file": ("workspace:read", "workspace:write"),
    "apply_patch": ("workspace:read", "workspace:write"),
    "atomic_write_file": ("workspace:read", "workspace:write"),
    "audit_events": ("workspace:read",),
    "browser_accessibility_snapshot": ("browser:use",),
    "browser_assert": ("browser:use",),
    "browser_check_errors": ("browser:use",),
    "browser_dom_snapshot": ("browser:use",),
    "browser_evaluate": ("browser:use",),
    "browser_form_snapshot": ("browser:use",),
    "browser_inspect": ("browser:use",),
    "browser_interact": ("browser:use",),
    "browser_network_trace": ("browser:use",),
    "browser_session_accessibility_snapshot": ("browser:use",),
    "browser_session_close": ("browser:use",),
    "browser_session_dom_snapshot": ("browser:use",),
    "browser_session_download": ("browser:use", "workspace:read", "workspace:write"),
    "browser_session_evaluate": ("browser:use",),
    "browser_session_frame_evaluate": ("browser:use",),
    "browser_session_import_storage": ("browser:use",),
    "browser_session_inspect": ("browser:use",),
    "browser_session_interact": ("browser:use",),
    "browser_session_list": ("browser:use",),
    "browser_session_logs": ("browser:use",),
    "browser_session_open": ("browser:use",),
    "browser_session_popup": ("browser:use",),
    "browser_session_route": ("browser:use",),
    "browser_session_trace": ("browser:use", "workspace:read", "workspace:write"),
    "browser_session_upload": ("browser:use", "workspace:read"),
    "browser_storage_state": ("browser:use",),
    "chmod_path": ("workspace:read", "workspace:write"),
    "close_project": ("workspace:read",),
    "command_history": ("command:run",),
    "compose_logs": ("command:run", "deploy:run", "workspace:read"),
    "compose_restart": ("command:run", "deploy:run", "workspace:read", "workspace:write"),
    "compose_status": ("command:run", "deploy:run", "workspace:read"),
    "copy_path": ("workspace:read", "workspace:write"),
    "create_directory": ("workspace:read", "workspace:write"),
    "create_snapshot": ("workspace:read", "workspace:write"),
    "create_symlink": ("workspace:read", "workspace:write"),
    "database_query": ("command:run", "database:use", "workspace:read"),
    "delete_path": ("workspace:read", "workspace:write"),
    "deployment_apply": ("deploy:run", "workspace:read", "workspace:write"),
    "deployment_preflight": ("deploy:run", "workspace:read", "workspace:write"),
    "deployment_rollback": ("deploy:run", "workspace:read", "workspace:write"),
    "diff_files": ("workspace:read",),
    "dns_lookup": ("network:fetch",),
    "environment_info": ("command:run", "workspace:read"),
    "fetch_url": ("network:fetch",),
    "file_hash": ("workspace:read",),
    "find_file": ("workspace:read",),
    "find_symbol": ("workspace:read",),
    "forget_process": ("command:run",),
    "get_process_output": ("command:run",),
    "get_project_info": ("workspace:read",),
    "git_blame": ("command:run", "workspace:read"),
    "git_branch": ("command:run", "workspace:read"),
    "git_checkout": ("command:run", "workspace:read", "workspace:write"),
    "git_commit": ("command:run", "workspace:read", "workspace:write"),
    "git_diff": ("command:run", "workspace:read"),
    "git_fetch": ("command:run", "workspace:read"),
    "git_log": ("command:run", "workspace:read"),
    "git_pull": ("command:run", "workspace:read", "workspace:write"),
    "git_push": ("command:run", "workspace:read", "workspace:write"),
    "git_restore": ("command:run", "workspace:read", "workspace:write"),
    "git_show": ("command:run", "workspace:read"),
    "git_staged_diff": ("command:run", "workspace:read"),
    "git_stash": ("command:run", "workspace:read", "workspace:write"),
    "git_status": ("command:run", "workspace:read"),
    "git_worktree": ("command:run", "workspace:read", "workspace:write"),
    "github_create_pull_request": ("github:write", "secrets:use"),
    "github_push_branch": ("github:write", "secrets:use", "workspace:read", "workspace:write"),
    "http_download": ("network:fetch", "workspace:read", "workspace:write"),
    "http_request": ("network:fetch",),
    "http_upload": ("network:fetch", "workspace:read"),
    "insert_at_line": ("workspace:read", "workspace:write"),
    "install_apt_packages": ("admin:install", "command:run", "workspace:read"),
    "install_node_packages": ("admin:install", "command:run", "workspace:read", "workspace:write"),
    "install_project_dependencies": ("admin:install", "command:run", "workspace:read", "workspace:write"),
    "install_python_packages": ("admin:install", "command:run", "workspace:read"),
    "list_files": ("workspace:read",),
    "list_processes": ("command:run",),
    "list_projects": ("workspace:read",),
    "list_snapshots": ("workspace:read",),
    "move_path": ("workspace:read", "workspace:write"),
    "open_project": ("workspace:read",),
    "port_owner": ("command:run",),
    "project_checkpoint": ("workspace:read", "workspace:write"),
    "project_context": ("workspace:read",),
    "project_memory_get": ("workspace:read",),
    "project_memory_set": ("workspace:read", "workspace:write"),
    "project_verify": ("command:run", "workspace:read"),
    "pwd": ("workspace:read",),
    "read_binary_file": ("workspace:read",),
    "read_file": ("workspace:read",),
    "read_files": ("workspace:read",),
    "read_symlink": ("workspace:read",),
    "regex_search": ("workspace:read",),
    "replace_in_file": ("workspace:read", "workspace:write"),
    "replace_lines": ("workspace:read", "workspace:write"),
    "restore_snapshot": ("workspace:read", "workspace:write"),
    "run_command": ("command:run", "workspace:read"),
    "run_command_advanced": ("command:run", "workspace:read"),
    "search_all_matches": ("workspace:read",),
    "search_files": ("workspace:read",),
    "self_improvement_readiness": ("workspace:read",),
    "send_process_input": ("command:run",),
    "signal_process": ("command:run",),
    "start_process": ("command:run", "workspace:read"),
    "start_process_advanced": ("command:run", "workspace:read"),
    "stat_path": ("workspace:read",),
    "stop_process": ("command:run",),
    "switch_project": ("workspace:read",),
    "tcp_check": ("network:fetch",),
    "tls_certificate": ("network:fetch",),
    "tree": ("workspace:read",),
    "wait_for_http_health": ("deploy:run", "workspace:read"),
    "wait_for_process_output": ("command:run",),
    "write_binary_file": ("workspace:read", "workspace:write"),
    "write_file": ("workspace:read", "workspace:write"),
    "resource_status": ("workspace:read",),
    "resource_cleanup": ("workspace:read", "command:run", "browser:use"),
}


def authorize_tool(tool_name: str) -> None:
    """Enforce the documented OAuth scope contract for a public MCP tool."""
    try:
        required = TOOL_SCOPE_REQUIREMENTS[tool_name]
    except KeyError as exc:
        raise RuntimeError(f"No authorization contract is defined for MCP tool: {tool_name}") from exc
    for scope in required:
        require_scope(scope)


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
        state = UserSessionState(
            subject=subject, projects={"workspace": root}, current_project_name="workspace", current_project=root
        )
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


def _audit_state(state: UserSessionState, event: str, details: dict[str, Any]) -> None:
    """Write a bounded, credential-free audit record for a known session state."""
    try:
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "subject": state.subject,
            "project": state.current_project_name,
            "event": event,
            "details": details,
        }
        digest = hashlib.sha256(state.subject.encode("utf-8")).hexdigest()[:24]
        target_root = AUDIT_ROOT / digest
        target_root.mkdir(parents=True, exist_ok=True)
        with (target_root / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    except Exception:
        # An audit-volume failure must never turn a safe operation into an outage.
        pass


def _audit(event: str, details: dict[str, Any]) -> None:
    """Write a bounded, credential-free audit record for the current identity."""
    _audit_state(session_state(), event, details)


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
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        output = stdout + "\n" + stderr
        return f"timed_out: true\nseconds: {timeout}\n\n{output[:MAX_OUTPUT]}"

    output = f"exit_code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    return output[:MAX_OUTPUT]


def _process_status(record: ProcessRecord) -> str:
    status = record.process.poll()
    if status is not None and record.exited_at is None:
        record.exited_at = time.time()
    return "running" if status is None else f"exited {status}"


def _touch_process(record: ProcessRecord) -> None:
    record.last_activity_at = time.time()


def _touch_browser(record: BrowserSessionRecord) -> None:
    record.last_activity_at = time.time()


def _redact_text(text: str, values: Iterable[str]) -> str:
    redacted = text
    for value in sorted({item for item in values if item}, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _record_process_line(record: ProcessRecord, line: str) -> None:
    with PROCESS_LOCK:
        record.total_lines += 1
        record.last_activity_at = time.time()
        record.output.append(_redact_text(line.rstrip("\n"), record.redactions))


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
            if record.exited_at is None:
                record.exited_at = time.time()


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
        summary["buttons"] = await page.locator(
            "button, [role=button], input[type=button], input[type=submit]"
        ).evaluate_all(
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


def _append_browser_events(
    page,
    console_messages: list[dict[str, Any]],
    page_errors: list[str],
    failed_requests: list[dict[str, Any]],
    responses: list[dict[str, Any]],
) -> None:
    page.on(
        "console",
        lambda msg: (
            console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location})
            if msg.type in {"error", "warning"}
            else None
        ),
    )
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on(
        "requestfailed",
        lambda req: failed_requests.append(
            {"url": req.url, "method": req.method, "failure": str(req.failure) if req.failure else ""}
        ),
    )
    page.on(
        "response",
        lambda resp: (
            responses.append({"url": resp.url, "status": resp.status, "status_text": resp.status_text})
            if resp.status >= 400
            else None
        ),
    )


def _attach_session_events(session_id: str, page, record: BrowserSessionRecord) -> None:
    page.on(
        "console",
        lambda msg: (
            record.console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location})
            if msg.type in {"error", "warning"}
            else None
        ),
    )
    page.on("pageerror", lambda exc: record.page_errors.append(str(exc)))
    page.on(
        "requestfailed",
        lambda req: record.failed_requests.append(
            {"url": req.url, "method": req.method, "failure": str(req.failure) if req.failure else ""}
        ),
    )
    page.on(
        "response",
        lambda resp: (
            record.responses.append({"url": resp.url, "status": resp.status, "status_text": resp.status_text})
            if resp.status >= 400
            else None
        ),
    )


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

    raise ValueError(
        "Action needs one locator field: selector, role/name, text, label, placeholder, test_id, alt_text, or title"
    )


async def _run_browser_action(page, action: dict[str, Any], index: int, timeout_ms: int) -> dict[str, Any]:
    kind = str(action.get("action", "")).strip().lower()
    value = action.get("value", "")
    result: dict[str, Any] = {"index": index, "action": kind}

    if kind in {"click", "dblclick", "fill", "type", "press", "hover", "check", "uncheck", "select", "focus", "blur"}:
        locator = await _locator_from_action(page, action)
        if kind == "click":
            await locator.click(
                button=str(action.get("button", "left")), click_count=_safe_int(action.get("click_count", 1), 1, 1, 10)
            )
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
        await page.wait_for_selector(
            str(action.get("selector")), timeout=timeout_ms, state=str(action.get("state", "visible"))
        )
    elif kind == "wait_for_text":
        locator = page.get_by_text(str(action.get("text", "")), exact=bool(action.get("exact", False)))
        await locator.wait_for(timeout=timeout_ms, state=str(action.get("state", "visible")))
    elif kind == "wait_for_url":
        await page.wait_for_url(str(action.get("url", "**")), timeout=timeout_ms)
    elif kind == "wait":
        await page.wait_for_timeout(_safe_int(action.get("ms", 1000), 1000, 1, timeout_ms))
    elif kind == "goto":
        await page.goto(
            _normalize_url(str(action.get("url"))), wait_until=str(action.get("wait_until", "load")), timeout=timeout_ms
        )
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


async def _evaluate_dom_snapshot(
    page, selector: str, element_limit: int, text_limit: int, include_attributes: bool
) -> dict[str, Any]:
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
    _touch_browser(record)
    return record


def _resource_counts() -> dict[str, int]:
    with SESSION_LOCK:
        states = list(SESSIONS.values())
    processes = sum(len(state.processes) for state in states)
    browsers = sum(len(state.browser_sessions) for state in states)
    running = sum(1 for state in states for record in state.processes.values() if record.process.poll() is None)
    return {"sessions": len(states), "processes": processes, "running_processes": running, "browser_sessions": browsers}


def _stop_process_record(record: ProcessRecord) -> None:
    if record.process.poll() is not None:
        if record.exited_at is None:
            record.exited_at = time.time()
        return
    try:
        os.killpg(record.process.pid, 15)
        record.process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(record.process.pid, 9)
            record.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
    finally:
        record.exited_at = time.time()


async def _close_browser_record(record: BrowserSessionRecord) -> list[str]:
    errors: list[str] = []
    for cleanup in (record.context.close, record.browser.close, record.playwright.stop):
        try:
            await cleanup()
        except Exception as exc:
            errors.append(type(exc).__name__)
    return errors


async def _cleanup_state_resources(
    state: UserSessionState, *, now: float | None = None, force: bool = False
) -> dict[str, Any]:
    current = time.time() if now is None else now
    removed_processes: list[str] = []
    stopped_processes: list[str] = []
    closed_browsers: list[str] = []
    cleanup_errors: list[dict[str, Any]] = []

    with PROCESS_LOCK:
        process_items = list(state.processes.items())
    for process_id, record in process_items:
        status = record.process.poll()
        if status is None:
            stale = PROCESS_IDLE_TTL_SECONDS > 0 and current - record.last_activity_at >= PROCESS_IDLE_TTL_SECONDS
            if force or stale:
                _stop_process_record(record)
                stopped_processes.append(process_id)
                with PROCESS_LOCK:
                    state.processes.pop(process_id, None)
                removed_processes.append(process_id)
        else:
            if record.exited_at is None:
                record.exited_at = current
            expired = (
                FINISHED_PROCESS_RETENTION_SECONDS > 0
                and current - record.exited_at >= FINISHED_PROCESS_RETENTION_SECONDS
            )
            if force or expired:
                with PROCESS_LOCK:
                    state.processes.pop(process_id, None)
                removed_processes.append(process_id)

    browser_items = list(state.browser_sessions.items())
    for session_id, record in browser_items:
        stale = BROWSER_IDLE_TTL_SECONDS > 0 and current - record.last_activity_at >= BROWSER_IDLE_TTL_SECONDS
        if force or stale:
            state.browser_sessions.pop(session_id, None)
            errors = await _close_browser_record(record)
            if errors:
                cleanup_errors.append({"type": "browser", "session_id": session_id, "errors": errors})
            closed_browsers.append(session_id)

    result = {
        "subject": state.subject,
        "removed_processes": removed_processes,
        "stopped_processes": stopped_processes,
        "closed_browser_sessions": closed_browsers,
        "errors": cleanup_errors,
    }
    if removed_processes or closed_browsers or cleanup_errors:
        _audit_state(
            state,
            "resource_cleanup",
            {
                "force": force,
                "removed_process_count": len(removed_processes),
                "stopped_process_count": len(stopped_processes),
                "closed_browser_count": len(closed_browsers),
                "error_count": len(cleanup_errors),
            },
        )
    return result


async def _cleanup_all_resources(*, force: bool = False) -> list[dict[str, Any]]:
    global RESOURCE_CLEANUP_LAST_ERROR, RESOURCE_CLEANUP_LAST_RUN
    with SESSION_LOCK:
        states = list(SESSIONS.values())
    results: list[dict[str, Any]] = []
    try:
        for state in states:
            results.append(await _cleanup_state_resources(state, force=force))
        RESOURCE_CLEANUP_LAST_ERROR = None
    except Exception as exc:
        RESOURCE_CLEANUP_LAST_ERROR = type(exc).__name__
    finally:
        RESOURCE_CLEANUP_LAST_RUN = time.time()
    return results


async def _resource_cleanup_loop() -> None:
    while True:
        interval = RESOURCE_CLEANUP_INTERVAL_SECONDS
        if interval <= 0:
            await asyncio.sleep(3600)
            continue
        await asyncio.sleep(interval)
        await _cleanup_all_resources()


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(_request: Request) -> JSONResponse:
    counts = _resource_counts()
    return JSONResponse(
        {
            "status": "ok",
            "workspace_root_available": WORKSPACE_ROOT.exists(),
            "cleanup_worker_enabled": RESOURCE_CLEANUP_INTERVAL_SECONDS > 0,
            "cleanup_last_run_unix": RESOURCE_CLEANUP_LAST_RUN,
            "cleanup_last_error": RESOURCE_CLEANUP_LAST_ERROR,
            **counts,
        }
    )


def _run_argv(argv: list[str], cwd: Path, timeout_seconds: int = 300) -> dict[str, Any]:
    """Run a fixed argv command and return bounded structured output."""
    try:
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout_seconds)
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
    commands: dict[str, list[list[str]]] = {
        "syntax": [],
        "test": [],
        "lint": [],
        "typecheck": [],
        "build": [],
        "compose": [],
    }
    pyproject = root / "pyproject.toml"
    if pyproject.exists() or (root / "requirements.txt").exists():
        commands["syntax"].append(["python", "-m", "compileall", "-q", "."])
    if (root / "tests").exists() or (root / "pytest.ini").exists() or pyproject.exists():
        commands["test"].append(["python", "-m", "pytest", "-q"])
    if pyproject.exists():
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.ruff" in pyproject_text:
            commands["lint"].extend(
                [
                    ["python", "-m", "ruff", "check", "."],
                    ["python", "-m", "ruff", "format", "--check", "."],
                ]
            )
        if "[tool.pyright]" in pyproject_text:
            commands["typecheck"].append(["python", "-m", "pyright"])
    if (root / "package.json").exists():
        for suite in ("test", "lint", "typecheck", "build"):
            commands[suite].append(["npm", "run", "--if-present", suite])
    if (root / ".mcp-compose-validation.env").exists() and (
        (root / "docker-compose.yml").exists() or (root / "compose.yml").exists()
    ):
        commands["compose"].append(
            ["docker", "compose", "--env-file", ".mcp-compose-validation.env", "config", "--quiet"]
        )
    return commands


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


def _command_environment(environment: dict[str, str] | None, secret_refs: list[str] | None) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(session_state().current_project)
    for name, value in (environment or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid environment variable name: {name}")
        env[name] = str(value)
    env.update(_secret_values(secret_refs or []))
    return env


__all__ = [
    "AGENT_POLICY_PATH",
    "AUDIT_ROOT",
    "AUTH_MODE",
    "AUTH_SETTINGS",
    "AccessToken",
    "Any",
    "AnyHttpUrl",
    "AuthSettings",
    "BROWSER_LOG_LIMIT",
    "BrowserSessionRecord",
    "DEFAULT_TIMEOUT_SECONDS",
    "FastMCP",
    "Iterable",
    "KEYCLOAK_REALM",
    "MAX_OUTPUT",
    "MAX_READ_BYTES",
    "MEMORY_ROOT",
    "OIDC_AUDIENCE",
    "OIDC_CLIENT_ID",
    "OIDC_ISSUER",
    "OIDC_JWKS_URL",
    "OidcTokenVerifier",
    "PROCESS_LOCK",
    "PROCESS_LOG_LIMIT",
    "PUBLIC_URL",
    "Path",
    "ProcessRecord",
    "READ_ONLY_ANNOTATIONS",
    "SECRET_REFS_PATH",
    "SELF_IMPROVEMENT_WORKSPACE",
    "SESSIONS",
    "SESSION_LOCK",
    "SNAPSHOT_ROOT",
    "TOKEN_VERIFIER",
    "TOOL_SCOPE_REQUIREMENTS",
    "TokenVerifier",
    "ToolAnnotations",
    "UTC",
    "UserSessionState",
    "WORKSPACE_MAP_PATH",
    "WORKSPACE_ROOT",
    "_append_browser_events",
    "_attach_session_events",
    "_audit",
    "_auth_settings",
    "_bounded_timeout_ms",
    "_collect_page_summary",
    "_command_environment",
    "_evaluate_accessibility_snapshot",
    "_evaluate_dom_snapshot",
    "_evaluate_form_snapshot",
    "_format_browser_result",
    "_get_browser_session",
    "_identity",
    "_identity_storage_root",
    "_is_allowed_path",
    "_json_result",
    "_load_playwright",
    "_load_project_memory",
    "_locator_from_action",
    "_new_browser_page",
    "_normalize_url",
    "_process_status",
    "_project_markers",
    "_project_memory_file",
    "_read_process_output",
    "_record_process_line",
    "_run_argv",
    "_run_browser_action",
    "_run_browser_actions",
    "_safe_int",
    "_save_project_memory",
    "_secret_values",
    "_verification_commands",
    "_workspace_map",
    "authorize_tool",
    "base64",
    "dataclass",
    "datetime",
    "deque",
    "difflib",
    "field",
    "get_access_token",
    "hashlib",
    "json",
    "mcp",
    "os",
    "re",
    "require_scope",
    "resolve_path",
    "session_state",
    "set_current_project",
    "shell",
    "shlex",
    "shutil",
    "socket",
    "ssl",
    "subprocess",
    "threading",
    "time",
    "urllib",
    "urlparse",
    "user_snapshot_root",
    "uuid",
]
