"""Public MCP server facade.

The runtime infrastructure lives in :mod:`app.core`; MCP tools are grouped by
capability under :mod:`app.tools`. Existing imports from ``app.server`` remain
supported for compatibility.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

_previous_core = globals().get("_core")
if isinstance(_previous_core, ModuleType):
    _core = importlib.reload(_previous_core)
else:
    from app import core as _core

_TOOL_MODULE_NAMES = [
    "app.tools.browser",
    "app.tools.commands",
    "app.tools.compose",
    "app.tools.database",
    "app.tools.deployment",
    "app.tools.files",
    "app.tools.git",
    "app.tools.github",
    "app.tools.network",
    "app.tools.packages",
    "app.tools.processes",
    "app.tools.project",
    "app.tools.resources",
    "app.tools.snapshots",
    "app.tools.workspaces",
]
_TOOL_MODULES: list[ModuleType] = []
for _module_name in _TOOL_MODULE_NAMES:
    if _module_name in sys.modules:
        _module = importlib.reload(sys.modules[_module_name])
    else:
        _module = importlib.import_module(_module_name)
    _TOOL_MODULES.append(_module)

_TOOL_EXPORTS = {name: getattr(module, name) for module in _TOOL_MODULES for name in module.TOOL_EXPORTS}

# The facade intentionally mirrors the historic module-level API.
for _name in _core.__all__:
    globals()[_name] = getattr(_core, _name)
globals().update(_TOOL_EXPORTS)


if __name__ == "__main__":
    _core.mcp.run(transport="streamable-http")
