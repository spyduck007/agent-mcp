"""Persistent command-environment profiles."""

from app.core import (
    _command_environment,
    _environment_profiles,
    _format_browser_result,
    _save_environment_profiles,
    authorize_tool,
    mcp,
    re,
)


@mcp.tool()
def environment_profile_list() -> str:
    """List named persistent command-environment profiles."""
    authorize_tool("environment_profile_list")
    profiles = _environment_profiles()
    return _format_browser_result({"profiles": sorted(profiles), "values": profiles})


@mcp.tool()
def environment_profile_set(name: str, environment: dict[str, str]) -> str:
    """Create or replace a persistent environment profile used by commands, processes, and terminals."""
    authorize_tool("environment_profile_set")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", name):
        raise ValueError("Invalid environment profile name")
    for key in environment:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid environment variable name: {key}")
    profiles = _environment_profiles()
    profiles[name] = {key: str(value) for key, value in environment.items()}
    _save_environment_profiles(profiles)
    return _format_browser_result({"saved": name, "profiles": sorted(profiles)})


@mcp.tool()
def environment_profile_delete(name: str) -> str:
    """Delete a persistent command-environment profile."""
    authorize_tool("environment_profile_delete")
    profiles = _environment_profiles()
    if name not in profiles:
        raise KeyError(f"Unknown environment profile: {name}")
    del profiles[name]
    _save_environment_profiles(profiles)
    return _format_browser_result({"deleted": name, "profiles": sorted(profiles)})


@mcp.tool()
def environment_profile_preview(
    name: str | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    """Preview the effective non-secret command environment for a profile."""
    authorize_tool("environment_profile_preview")
    env = _command_environment(environment=environment, profile=name)
    safe = {
        key: value
        for key, value in env.items()
        if key
        in {"HOME", "PATH", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "VIRTUAL_ENV", "GOROOT", "GOPATH"}
        or key.startswith("AGENT_")
    }
    return _format_browser_result({"profile": name, "environment": safe})


TOOL_EXPORTS = [
    "environment_profile_list",
    "environment_profile_set",
    "environment_profile_delete",
    "environment_profile_preview",
]
