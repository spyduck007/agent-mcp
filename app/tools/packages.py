"""Package-management tools for system, language, and project dependencies."""

from __future__ import annotations

import shutil

from app.core import (
    TOOL_ROOT,
    _command_environment,
    _format_browser_result,
    _run_argv,
    authorize_tool,
    mcp,
    resolve_path,
    session_state,
)


def _run(argv: list[str], timeout: int = 3600, cwd: str = ".") -> str:
    return _format_browser_result(_run_argv(argv, resolve_path(cwd), timeout))


@mcp.tool()
def install_apt_packages(packages: list[str]) -> str:
    """Install Debian packages inside the MCP container."""
    authorize_tool("install_apt_packages")
    if not packages:
        raise ValueError("packages is required")
    updated = _run(["apt-get", "update"], 1800)
    installed = _run(["apt-get", "install", "-y", "--no-install-recommends", *packages], 7200)
    return f"{updated}\n\n{installed}"


@mcp.tool()
def install_python_packages(packages: list[str], target: str = "agent") -> str:
    """Install Python packages into the persistent agent venv, current project venv, or MCP runtime."""
    authorize_tool("install_python_packages")
    if not packages:
        raise ValueError("packages is required")
    if target == "agent":
        venv = TOOL_ROOT / "venv"
        if not (venv / "bin/python").exists():
            created = _run_argv(["python", "-m", "venv", str(venv)], session_state().current_project, 600)
            if created["exit_code"] != 0:
                return _format_browser_result(created)
        argv = [str(venv / "bin/python"), "-m", "pip", "install", *packages]
    elif target == "project":
        venv = session_state().current_project / ".venv"
        if not (venv / "bin/python").exists():
            created = _run_argv(["python", "-m", "venv", str(venv)], session_state().current_project, 600)
            if created["exit_code"] != 0:
                return _format_browser_result(created)
        argv = [str(venv / "bin/python"), "-m", "pip", "install", *packages]
    elif target == "runtime":
        argv = ["python", "-m", "pip", "install", *packages]
    else:
        raise ValueError("target must be agent, project, or runtime")
    return _format_browser_result(_run_argv(argv, session_state().current_project, 7200))


@mcp.tool()
def install_node_packages(
    packages: list[str], dev: bool = False, package_manager: str = "npm", global_install: bool = False
) -> str:
    """Install Node packages in the active project or globally."""
    authorize_tool("install_node_packages")
    if package_manager == "npm":
        argv = ["npm", "install"]
        if global_install:
            argv.append("--global")
        elif dev:
            argv.append("--save-dev")
    elif package_manager == "pnpm":
        argv = ["pnpm", "add"]
        if global_install:
            argv.append("--global")
        elif dev:
            argv.append("-D")
    elif package_manager == "yarn":
        argv = ["yarn", "global", "add"] if global_install else ["yarn", "add"]
        if dev and not global_install:
            argv.append("-D")
    else:
        raise ValueError("package_manager must be npm, pnpm, or yarn")
    return _run([*argv, *packages], 7200)


@mcp.tool()
def package_install(
    manager: str,
    packages: list[str],
    global_install: bool = True,
    cwd: str = ".",
) -> str:
    """Install packages with apt, pip, uv, pipx, npm, pnpm, yarn, cargo, go, gem, composer, or sdkman."""
    authorize_tool("package_install")
    if not packages:
        raise ValueError("packages is required")
    manager = manager.lower()
    if manager == "apt":
        return install_apt_packages(packages)
    if manager == "pip":
        return install_python_packages(packages, target="agent" if global_install else "project")
    commands = {
        "uv": ["uv", "tool", "install"] if global_install else ["uv", "pip", "install"],
        "pipx": ["pipx", "install"],
        "npm": ["npm", "install", "--global"] if global_install else ["npm", "install"],
        "pnpm": ["pnpm", "add", "--global"] if global_install else ["pnpm", "add"],
        "yarn": ["yarn", "global", "add"] if global_install else ["yarn", "add"],
        "cargo": ["cargo", "install"],
        "go": ["go", "install"],
        "gem": ["gem", "install"],
        "composer": ["composer", "global", "require"] if global_install else ["composer", "require"],
        "sdkman": ["bash", "-lc", "source /root/.sdkman/bin/sdkman-init.sh && sdk install "],
    }
    if manager not in commands:
        raise ValueError("Unsupported package manager")
    if manager == "sdkman":
        command = commands[manager][-1] + " ".join(packages)
        return _run(["bash", "-lc", command], 7200, cwd)
    return _run([*commands[manager], *packages], 7200, cwd)


@mcp.tool()
def package_remove(manager: str, packages: list[str], global_install: bool = True, cwd: str = ".") -> str:
    """Remove packages with a supported package manager."""
    authorize_tool("package_remove")
    commands = {
        "apt": ["apt-get", "purge", "-y"],
        "pip": [str(TOOL_ROOT / "venv/bin/python"), "-m", "pip", "uninstall", "-y"],
        "pipx": ["pipx", "uninstall"],
        "npm": ["npm", "uninstall", "--global"] if global_install else ["npm", "uninstall"],
        "pnpm": ["pnpm", "remove", "--global"] if global_install else ["pnpm", "remove"],
        "yarn": ["yarn", "global", "remove"] if global_install else ["yarn", "remove"],
        "cargo": ["cargo", "uninstall"],
        "gem": ["gem", "uninstall", "-aIx"],
    }
    if manager not in commands:
        raise ValueError("Unsupported package manager")
    return _run([*commands[manager], *packages], 7200, cwd)


@mcp.tool()
def package_which(command: str) -> str:
    """Locate a command in the effective agent PATH and report its version when possible."""
    authorize_tool("package_which")
    env = _command_environment()
    path = shutil.which(command, path=env.get("PATH"))
    if path is None:
        return _format_browser_result({"command": command, "found": False})
    version = _run_argv([path, "--version"], session_state().current_project, 30)
    return _format_browser_result({"command": command, "found": True, "path": path, "version": version})


@mcp.tool()
def install_project_dependencies(package_manager: str | None = None) -> str:
    """Install dependencies for the active project using its lockfile or manifest."""
    authorize_tool("install_project_dependencies")
    project = session_state().current_project
    if package_manager is None:
        if (project / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (project / "yarn.lock").exists():
            package_manager = "yarn"
        elif (project / "package-lock.json").exists() or (project / "package.json").exists():
            package_manager = "npm"
        elif (project / "requirements.txt").exists():
            package_manager = "pip"
        elif (project / "pyproject.toml").exists():
            package_manager = "editable"
        else:
            raise ValueError("Could not detect project dependency manager")
    commands = {
        "npm": ["npm", "install"],
        "pnpm": ["pnpm", "install"],
        "yarn": ["yarn", "install"],
        "pip": ["python", "-m", "pip", "install", "-r", "requirements.txt"],
        "editable": ["python", "-m", "pip", "install", "-e", "."],
    }
    if package_manager not in commands:
        raise ValueError("Unsupported package manager")
    return _run(commands[package_manager], 7200)


TOOL_EXPORTS = [
    "install_apt_packages",
    "install_python_packages",
    "install_node_packages",
    "install_project_dependencies",
    "package_install",
    "package_remove",
    "package_which",
]
