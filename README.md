# Agent MCP

Agent MCP is a containerized Model Context Protocol (MCP) server for ChatGPT on the web. It exposes a project-oriented toolset for file editing, shell commands, long-running process management, Git helpers, browser inspection with Playwright, and project snapshots.

The server is implemented with `FastMCP` and runs over streamable HTTP at `/mcp`.

## Features

- Open and switch between assigned workspace directories.
- Read, write, append, patch, move, copy, delete, and search files.
- Run one-off shell commands with timeouts.
- Start, inspect, wait on, stop, and forget long-running background processes.
- Inspect Git status, diffs, logs, branches, checkout, and commit local changes.
- Fetch URLs from inside the container.
- Run browser automation with Playwright Chromium.
- Capture DOM, accessibility, form, network, storage, console, and error snapshots.
- Keep persistent browser sessions across multiple MCP calls.
- Track process and browser activity with configurable per-user limits and idle cleanup.
- Expose a credential-free `/health` endpoint for Docker readiness checks.
- Create, restore, and list local project snapshots.
- Install Debian, Python, and Node packages in the running container.
- Report grounded project context, stack markers, verification commands, and durable project memory.
- Keep credential-free audit events and resumable work checkpoints per authenticated identity.
- Run Compose deployment preflight, user-gated apply, health checks, and snapshot-backed rollback.
- Push a branch and create GitHub pull requests with an ephemeral named token reference.

## Repository Layout

```text
.
├── app/
│   ├── __init__.py
│   ├── core.py                 # authentication, shared state, paths, and helpers
│   ├── server.py               # compatibility facade and MCP entry point
│   └── tools/                  # capability-focused MCP tool modules
│       ├── browser.py
│       ├── commands.py
│       ├── compose.py
│       ├── database.py
│       ├── deployment.py
│       ├── files.py
│       ├── git.py
│       ├── github.py
│       ├── network.py
│       ├── packages.py
│       ├── processes.py
│       ├── project.py
│       ├── snapshots.py
│       └── workspaces.py
├── tests/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

`app.server` preserves the original public import surface, while each tool implementation lives in the module matching its capability. Shared authorization, session state, path validation, browser helpers, process helpers, and configuration remain centralized in `app.core` so every tool group uses the same security boundary.

## Requirements

For Docker usage:

- Docker
- Docker Compose

For local Python usage:

- Python 3.12 or newer
- `pip`
- Playwright browser dependencies

Docker is the recommended way to run this project because the image installs the tools the MCP server exposes to agents, including Git, Docker CLI, Node, npm, pnpm, Yarn, Go, Rust, Java, ripgrep, SQLite, PostgreSQL client tools, and Chromium for Playwright.

## Web deployment (required for ChatGPT)

This server is deliberately fail-closed. Its normal Compose deployment includes a self-hosted Keycloak OAuth/OIDC provider, HTTPS, and an explicit identity-to-workspace map. It does **not** mount SSH keys or the host home directory. The production deployment intentionally mounts the Docker socket for authenticated deployment tools; that is root-equivalent host access.

1. Point a public DNS name at this machine and copy the environment template:

```bash
cp .env.example .env
mkdir -p config workspaces snapshots
cp config/workspaces.example.json config/workspaces.json
```

2. Edit `.env` with your public domain and four long random secrets. Keycloak is self-hosted in this Compose stack; no third-party OAuth account or endpoints are needed. Generate secrets with:

```bash
openssl rand -base64 32
```

The bootstrap service creates a `chatgpt` login user (or the username set in `KEYCLOAK_MCP_USERNAME`) and grants it these roles:

   - `workspace:read`
   - `workspace:write`
   - `command:run`
   - `browser:use`
   - `network:fetch`
   - `secrets:use`
   - `database:use`
   - `admin:install`
   - `deploy:run`
   - `github:write`

3. The Keycloak bootstrap service replaces `KEYCLOAK_MCP_SUBJECT` in `config/workspaces.json` with the actual subject of the configured login user. Create its project directory:

```bash
mkdir -p workspaces/chatgpt-project
```

Every listed workspace path must remain inside `/workspaces` in the container.

4. Start it:

```bash
docker compose --profile production up -d --build
docker compose --profile production logs -f
```

The production stack binds the MCP server to `127.0.0.1:8081` and Keycloak to `127.0.0.1:8082`. Put an existing HTTPS Nginx reverse proxy in front of them: proxy `/mcp` to `8081` and `/auth/` to `8082` without stripping the `/auth` prefix. Copy the locations from [nginx/mcp.locations.conf.example](nginx/mcp.locations.conf.example) into the existing HTTPS server block, including the protected-resource metadata routes, then validate and reload Nginx. Keycloak is then available at `https://your-domain.example/auth`; it issues and signs the JWTs validated by the MCP server.

### ChatGPT setup

In ChatGPT Developer mode, add a remote streaming-HTTP MCP app with:

| Field | Value |
| --- | --- |
| Server URL | `https://your-domain.example/mcp` |
| Authentication | OAuth |
| Client registration | Static |
| Client ID | `chatgpt-agent-mcp` |
| Client secret | Value of `KEYCLOAK_CHATGPT_CLIENT_SECRET` from `.env` |
| Token endpoint auth method | `client_secret_post` |
| Authorization URL | `https://your-domain.example/auth/realms/agent-mcp/protocol/openid-connect/auth` |
| Token URL | `https://your-domain.example/auth/realms/agent-mcp/protocol/openid-connect/token` |
| Scopes | `openid profile` |

When ChatGPT opens the login page, sign in with `KEYCLOAK_MCP_USERNAME` and `KEYCLOAK_MCP_PASSWORD` from `.env`. If ChatGPT displays a specific callback URL, add that exact URL to the client redirect URIs in Keycloak Admin Console (`/auth/admin`); the bootstrap configuration already permits standard `chatgpt.com` callback paths.

ChatGPT supports remote streaming HTTP MCP servers and OAuth authentication. [OpenAI’s setup guide](https://developers.openai.com/api/docs/guides/developer-mode#how-to-use)

For local-only development, run with `AUTH_MODE=disabled` only on a loopback-bound port. Never use that mode on an internet-accessible server.

## Controlled self-improvement setup

The agent improves this MCP from an isolated clone under `/workspaces/agent-mcp`, never from the live deployment checkout. The clone contains no `.env`, Keycloak data, Docker volumes, or host credentials.

After this version is deployed, run these host commands from the repository root:

```bash
./scripts/setup-self-improvement-workspace.sh
cp config/secrets.example.json config/secrets.json
chmod 600 config/secrets.json
```

Create a **fine-grained GitHub token** for this repository only, with `Contents: Read and write` and `Pull requests: Read and write`. Put it in the ignored `config/secrets.json` as the `GITHUB_TOKEN` value. Do not put it in `.env`, the project clone, a prompt, or a ChatGPT configuration field.

Then opt in to the dedicated workspace in the ignored `.env`:

```text
ENABLE_SELF_IMPROVEMENT_WORKSPACE=true
SELF_IMPROVEMENT_WORKSPACE=agent-mcp
```

Apply the change and grant the existing Keycloak user the new `github:write` role:

```bash
docker compose --profile production up -d --build
# The bootstrap service is one-shot, so recreate it explicitly after changing .env.
docker compose --profile production up -d --force-recreate keycloak-bootstrap
docker compose --profile production restart mcp-server
```

Disconnect and reconnect the ChatGPT MCP app once after this step so its access token contains the new role. The ChatGPT OAuth fields and scopes remain unchanged: use only `openid profile`.

In ChatGPT, start a self-improvement task with:

```text
Open the agent-mcp project workspace. First call self_improvement_readiness and project_context.
Create a new agent/<short-description> branch. Inspect before editing. Implement the requested change,
run project_verify, inspect git diff, create a project_checkpoint, then push the branch and open a PR.
Never merge or deploy unless I explicitly provide the approval phrase requested by the deployment tool.
```

The policy at `config/agent-policy.md` is supplied in `project_context`. It requires branch/PR workflow, grounded verification, no secret disclosure, and explicit user approval for deployment or rollback. You may customize the policy, but never store secrets in it.

### High-level agent tools

- `project_context`: grounded stack, root files, Git state, verification suites, policy, and memory keys.
- `project_verify`: fixed syntax/test/lint/typecheck/build/Compose checks with structured evidence.
- `project_memory_set` / `project_memory_get`: durable project decisions outside the repository.
- `project_checkpoint` / `audit_events`: resumable work state and credential-free action history.
- `self_improvement_readiness`: verifies the isolated clone and reports remaining prerequisites.
- `deployment_preflight`, `deployment_apply`, `deployment_rollback`: Compose validation, snapshot-backed recovery, and explicit approval gates.
- `github_push_branch`, `github_create_pull_request`: ephemeral-token GitHub workflow. Tokens are never written to Git config or returned in tool output.

## Local-only development

Build and run the MCP server:

```bash
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

This override binds the insecure development server to loopback only:

```text
http://localhost:8081/mcp
```

The container listens on port `8080`; the host mapping is `8081:8080`.

## MCP Client Configuration

Use a streamable HTTP MCP connection pointed at:

```text
http://localhost:8081/mcp
```

The server name is:

```text
coding-agent-mcp
```

For ChatGPT web, use the HTTPS URL from the web-deployment section. The loopback URL is for local development only.

## Docker Compose Details

The included `docker-compose.yml` starts one service named `mcp-server`.

Important mounts:

- `./workspaces:/workspaces` is the only project tree exposed to the service.
- `./config:/config:ro` holds the identity-to-workspace mapping.
- `./snapshots:/snapshots` stores project snapshots outside the container.

The production MCP service mounts the Docker socket so authenticated deployment tools can inspect and control host Docker. This is root-equivalent host access; keep OAuth credentials protected and do not expose this server beyond trusted users. SSH keys and the host home directory are not mounted.

Important production environment values are in `.env`: `PUBLIC_URL`, `WORKSPACES_DIR`, and the `KEYCLOAK_*` passwords. The server derives the issuer and JWKS endpoint from the public URL and refuses to start in production mode without a valid HTTPS URL.

### Resource lifecycle configuration

Tracked background processes and persistent browser sessions use generous configurable limits so long coding, debugging, and security workflows remain available without allowing abandoned resources to accumulate forever:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `MAX_PROCESSES_PER_USER` | `32` | Maximum tracked processes per authenticated identity |
| `MAX_BROWSER_SESSIONS_PER_USER` | `12` | Maximum persistent browser sessions per identity |
| `PROCESS_IDLE_TTL_SECONDS` | `14400` | Stop a running process after four hours without output or interaction |
| `BROWSER_IDLE_TTL_SECONDS` | `7200` | Close a browser session after two hours without interaction |
| `FINISHED_PROCESS_RETENTION_SECONDS` | `3600` | Keep exited-process output available for one hour |
| `RESOURCE_CLEANUP_INTERVAL_SECONDS` | `60` | Frequency of automatic cleanup checks |

Set any limit or expiration value to `0` to disable that behavior. Resource limits do not inspect or restrict command contents, browser destinations, programming languages, debuggers, compilers, or security tooling.

Cleanup runs automatically according to the configured limits and TTLs. On container shutdown, the MCP lifespan gracefully terminates tracked process groups and closes browser resources.

Docker checks `GET /health`, which returns only bounded service and resource counts—never commands, file names, browser URLs, credentials, or secret values.

## Local Development

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Run the server locally:

```bash
python -m app.server
```

Run the same verification checks used by CI:

```bash
python -m compileall -q .
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest -q
docker compose --env-file .mcp-compose-validation.env config --quiet
```

Pytest enforces the current 45% coverage floor. `project_verify` reports each requested suite as `passed`, `failed`, or `not_configured`; explicitly requesting an unconfigured suite makes the overall verification fail instead of silently passing.

By default, the server binds to `0.0.0.0:8080` and serves MCP traffic at `/mcp`.

## Tool Groups

### Project Tools

- `open_project`
- `switch_project`
- `list_projects`
- `close_project`
- `pwd`
- `get_project_info`
- `environment_info`
- `project_context`
- `project_memory_set`
- `project_memory_get`
- `project_checkpoint`
- `project_verify`
- `self_improvement_readiness`
- `audit_events`

### File and Search Tools

- `write_file`
- `append_file`
- `read_file`
- `read_files`
- `replace_in_file`
- `replace_lines`
- `insert_at_line`
- `copy_path`
- `move_path`
- `delete_path`
- `create_directory`
- `list_files`
- `tree`
- `find_file`
- `search_files`
- `regex_search`
- `find_symbol`
- `stat_path`
- `apply_patch`
- `file_hash`
- `read_binary_file`
- `write_binary_file`
- `atomic_write_file`
- `diff_files`
- `search_all_matches`
- `chmod_path`
- `create_symlink`
- `read_symlink`

### Shell and Process Tools

- `run_command`
- `command_history`
- `start_process`
- `list_processes`
- `get_process_output`
- `wait_for_process_output`
- `stop_process`
- `forget_process`
- `run_command_advanced`
- `start_process_advanced`
- `send_process_input`
- `signal_process`
- `port_owner`

### Git Tools

- `git_status`
- `git_diff`
- `git_log`
- `git_branch`
- `git_checkout`
- `git_commit`
- `git_restore`
- `git_staged_diff`
- `git_show`
- `git_blame`
- `git_fetch`
- `git_pull`
- `git_push`
- `git_stash`
- `git_worktree`

### Browser Tools

- `browser_inspect`
- `browser_check_errors`
- `browser_interact`
- `browser_evaluate`
- `browser_dom_snapshot`
- `browser_accessibility_snapshot`
- `browser_form_snapshot`
- `browser_assert`
- `browser_network_trace`
- `browser_storage_state`
- `browser_session_open`
- `browser_session_list`
- `browser_session_close`
- `browser_session_inspect`
- `browser_session_interact`
- `browser_session_evaluate`
- `browser_session_dom_snapshot`
- `browser_session_accessibility_snapshot`
- `browser_session_logs`
- `browser_session_upload`
- `browser_session_frame_evaluate`
- `browser_session_route`
- `browser_session_trace`
- `browser_session_import_storage`
- `browser_session_download`
- `browser_session_popup`

### Snapshot Tools

- `create_snapshot`
- `restore_snapshot`
- `list_snapshots`

### Dependency Tools

- `install_apt_packages`
- `install_python_packages`
- `install_node_packages`
- `install_project_dependencies`

### Network, Database, and Deployment Tools

- `http_request`
- `http_download`
- `http_upload`
- `dns_lookup`
- `tcp_check`
- `tls_certificate`
- `database_query`
- `compose_status`
- `compose_logs`
- `compose_restart`
- `wait_for_http_health`
- `deployment_preflight`
- `deployment_apply`
- `deployment_rollback`
- `github_push_branch`
- `github_create_pull_request`

## Browser Action Format

Browser interaction tools accept action dictionaries. Locator fields can use one of:

- `selector`
- `role` with optional `name`
- `text`
- `label`
- `placeholder`
- `test_id`
- `alt_text`
- `title`

Supported actions include:

- `click`
- `dblclick`
- `fill`
- `type`
- `press`
- `hover`
- `check`
- `uncheck`
- `select`
- `focus`
- `blur`
- `wait_for_selector`
- `wait_for_text`
- `wait_for_url`
- `wait`
- `goto`
- `reload`
- `assert_text`
- `assert_selector`
- `assert_count`
- `assert_url`
- `assert_title`
- `evaluate`

Example action payload:

```json
[
  {
    "action": "fill",
    "label": "Search",
    "value": "agent mcp"
  },
  {
    "action": "press",
    "label": "Search",
    "value": "Enter"
  },
  {
    "action": "assert_text",
    "text": "Results"
  }
]
```

## Snapshots

Snapshots are stored under `/snapshots` in the container and mapped to `./snapshots` by Docker Compose. A snapshot copies the active project while ignoring common generated directories such as `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, and `build`.

## Security Notes

This server is intentionally powerful. OAuth scopes restrict tools, but `command:run` permits arbitrary commands within the mounted workspace, and the mounted Docker socket is root-equivalent host access. Give these roles only to identities you trust. Keep SSH keys and broad host mounts out of the container. Use the isolated self-improvement clone and the policy/PR workflow for changes to this MCP.

Review the host paths in `docker-compose.yml` before sharing or deploying this project. The defaults are tailored to the original development machine and may expose more of the host filesystem than you want.

## Configuration

Most behavior is currently configured in `app/server.py` and Compose environment variables:

- `WORKSPACE_ROOT=/workspaces`
- `WORKSPACE_MAP_PATH=/config/workspaces.json`
- `SECRET_REFS_PATH=/config/secrets.json`
- `SNAPSHOT_ROOT=/snapshots`
- `PROCESS_LOG_LIMIT=5000`
- `BROWSER_LOG_LIMIT=500`
- `MAX_READ_BYTES=500000`
- `MAX_OUTPUT=80000`
- `DEFAULT_TIMEOUT_SECONDS=30`

For named secret injection, copy `config/secrets.example.json` to `config/secrets.json`, replace its values, and keep that file out of Git. Tools can request a secret by its reference name through `secret_refs`; values are injected into the child process only and are not returned by the MCP server. The self-hosted Keycloak role `secrets:use` is required. `database:use` and `deploy:run` govern database and Compose primitives.

The Docker image sets:

- `PYTHONDONTWRITEBYTECODE=1`
- `PYTHONUNBUFFERED=1`
- `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`

## Development Workflow

Useful checks:

```bash
python -m compileall app
```

Run the container:

```bash
docker compose up --build
```

Check the exposed endpoint from the host:

```bash
curl http://localhost:8081/mcp
```

The MCP endpoint may return a protocol-specific response depending on the request; this command is primarily a connectivity check.

## License

No license file is included yet. Add one before publishing this project for reuse by other people or organizations.
