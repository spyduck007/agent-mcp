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

This server is deliberately fail-closed. Its normal Compose deployment includes a self-hosted Keycloak OAuth/OIDC provider, HTTPS, and an explicit identity-to-workspace map. It does **not** mount your SSH keys, Docker socket, or home directory.

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

The production stack binds the MCP server to `127.0.0.1:8081` and Keycloak to `127.0.0.1:8082`. Put an existing HTTPS Nginx reverse proxy in front of them: proxy `/mcp` to `8081` and `/auth/` to `8082` without stripping the `/auth` prefix. Copy the locations from [nginx/mcp.locations.conf.example](nginx/mcp.locations.conf.example) into the existing HTTPS server block, then validate and reload Nginx. Keycloak is then available at `https://your-domain.example/auth`; it issues and signs the JWTs validated by the MCP server.

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

Important production environment values are in `.env`: `PUBLIC_URL`, `WORKSPACES_DIR`, and the `KEYCLOAK_*` passwords. The server derives the issuer and JWKS endpoint from the public URL and refuses to start in production mode without a valid HTTPS URL.

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
