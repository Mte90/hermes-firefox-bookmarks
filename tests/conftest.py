"""Shared test bootstrap: offline stub for the Hermes host module ``tools.registry``.

Inside the Hermes venv the real module wins; anywhere else a minimal stub makes
the suite runnable with plain python3 + pytest. Also keeps the env-login path
hermetic when the host machine exports FFB_* credentials.
"""
from __future__ import annotations

import json
import os
import sys
import types


def _install_registry_stub() -> None:
    registry = types.ModuleType("tools.registry")

    def tool_result(payload) -> str:
        return json.dumps(payload)

    def tool_error(message: str) -> str:
        return json.dumps({"error": message})

    registry.tool_result = tool_result
    registry.tool_error = tool_error
    pkg = types.ModuleType("tools")
    pkg.registry = registry
    sys.modules.setdefault("tools", pkg)
    sys.modules.setdefault("tools.registry", registry)


try:
    import tools.registry  # noqa: F401
except Exception:
    # "tools" may have resolved to the repo-root tools.py (not a package) —
    # discard it so the stub registers cleanly.
    sys.modules.pop("tools", None)
    _install_registry_stub()

for _var in ("FFB_EMAIL", "FFB_PASSWORD"):
    os.environ.pop(_var, None)
