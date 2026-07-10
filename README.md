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
- Create, restore, and list local project snapshots.
- Install Debian, Python, and Node packages in the running container.
- Report environment and project metadata.

## Repository Layout

```text
.
├── app/
│   ├── __init__.py
│   └── server.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

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

This server is deliberately fail-closed. Its normal Compose deployment requires HTTPS, OAuth/OIDC access tokens, and an explicit identity-to-workspace map. It does **not** mount your SSH keys, Docker socket, or home directory.

1. Point a public DNS name at this machine and copy the environment template:

```bash
cp .env.example .env
mkdir -p config workspaces snapshots
cp config/workspaces.example.json config/workspaces.json
```

2. Edit `.env` with the public domain and your OAuth/OIDC issuer, access-token audience, and JWKS URL. Configure that issuer to mint JWT access tokens containing a stable `sub` claim and these scopes as appropriate:

   - `workspace:read`
   - `workspace:write`
   - `command:run`
   - `browser:use`
   - `network:fetch`
   - `admin:install` (only for the package-install tools)

3. Edit `config/workspaces.json`, replacing each example subject with the exact `sub` from the identity provider. Every listed path must be inside `/workspaces` in the container, so use paths such as `/workspaces/alice-project`.

4. Start it:

```bash
docker compose --profile production up -d --build
docker compose --profile production logs -f
```

Caddy obtains and renews the TLS certificate once `MCP_DOMAIN` resolves publicly. Add `https://your-domain.example/mcp` as a remote streaming-HTTP MCP app in ChatGPT Developer mode, then complete the OAuth flow. ChatGPT supports remote streaming HTTP MCP servers and OAuth authentication. [OpenAI’s setup guide](https://developers.openai.com/api/docs/guides/developer-mode#how-to-use)

For local-only development, run with `AUTH_MODE=disabled` only on a loopback-bound port. Never use that mode on an internet-accessible server.

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

The Docker socket, SSH keys, and host home directory are intentionally not mounted.

Important production environment values are in `.env`: `PUBLIC_URL`, `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`, and `WORKSPACES_DIR`. The server refuses to start in production mode without the OAuth values.

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

### Shell and Process Tools

- `run_command`
- `command_history`
- `start_process`
- `list_processes`
- `get_process_output`
- `wait_for_process_output`
- `stop_process`
- `forget_process`

### Git Tools

- `git_status`
- `git_diff`
- `git_log`
- `git_branch`
- `git_checkout`
- `git_commit`

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

### Snapshot Tools

- `create_snapshot`
- `restore_snapshot`
- `list_snapshots`

### Dependency Tools

- `install_apt_packages`
- `install_python_packages`
- `install_node_packages`
- `install_project_dependencies`

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

This server is intentionally powerful. OAuth scopes restrict tools, but `command:run` permits arbitrary commands within the mounted workspace. Give that scope only to identities you trust. Do not add the Docker socket, SSH keys, or broad host mounts unless you deliberately accept their host-level consequences.

Review the host paths in `docker-compose.yml` before sharing or deploying this project. The defaults are tailored to the original development machine and may expose more of the host filesystem than you want.

## Configuration

Most behavior is currently configured in `app/server.py`:

- `HOST_ROOT=/host`
- `SNAPSHOT_ROOT=/snapshots`
- `PROCESS_LOG_LIMIT=5000`
- `BROWSER_LOG_LIMIT=500`
- `MAX_READ_BYTES=500000`
- `MAX_OUTPUT=80000`
- `DEFAULT_TIMEOUT_SECONDS=30`

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
