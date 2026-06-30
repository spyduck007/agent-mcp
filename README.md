# Agent MCP

Agent MCP is a containerized Model Context Protocol (MCP) server for coding agents. It exposes a project-oriented toolset for file editing, shell commands, long-running process management, Git helpers, HTTP fetches, browser inspection with Playwright, package installation, and project snapshots.

The server is implemented with `FastMCP` and runs over streamable HTTP at `/mcp`.

## Features

- Open and switch between host project directories.
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

## Quick Start

Build and run the MCP server:

```bash
docker compose up --build
```

The compose file maps the MCP server to:

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

Exact MCP client configuration differs by client. Use streamable HTTP as the transport and the URL above as the endpoint.

## Docker Compose Details

The included `docker-compose.yml` starts one service named `mcp-server`.

Important mounts:

- `/home/anshagrawal:/host` exposes the host workspace tree inside the container.
- `./app:/app/app` enables live editing of the server source while developing.
- `./snapshots:/snapshots` stores project snapshots outside the container.
- `/var/run/docker.sock:/var/run/docker.sock` lets tools inside the container talk to the host Docker daemon.
- `/home/anshagrawal/.ssh:/root/.ssh:ro` exposes SSH credentials read-only for Git operations.

Important environment values:

- `HOME=/host` makes agent shell commands use the mounted host tree as home.
- `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` uses the Chromium install baked into the image.
- `GIT_SSH_COMMAND` selects the mounted SSH key and accepts new host keys.
- `NO_PROXY`/`no_proxy` keep localhost and host gateway traffic local.

If you use this project on another machine, update the host-specific paths in `docker-compose.yml` before running it.

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

This server is intentionally powerful. It can read and write mounted files, run shell commands, install packages, access the Docker socket, use mounted SSH credentials, and automate browsers. Run it only in trusted environments and expose the MCP endpoint only to trusted clients.

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
