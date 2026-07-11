"""MCP tools for the packages capability group."""

from app.core import (
    authorize_tool,
    mcp,
    require_scope,
    session_state,
    shell,
    shlex,
)


@mcp.tool()
def install_apt_packages(packages: list[str]) -> str:
    """Install Debian packages inside the MCP container."""
    authorize_tool("install_apt_packages")
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
    authorize_tool("install_python_packages")
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
    authorize_tool("install_node_packages")
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
def install_project_dependencies(package_manager: str | None = None) -> str:
    """Install dependencies for the active project."""
    authorize_tool("install_project_dependencies")
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


TOOL_EXPORTS = [
    "install_apt_packages",
    "install_python_packages",
    "install_node_packages",
    "install_project_dependencies",
]
