"""Register the code-conduit plugin as a Python package for tests.

Follows the unifi conftest pattern — multi-module plugin with
intra-package relative imports. Critically: NO
``submodule_search_locations`` kwarg, per the unifi gotcha doc.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_code_conduit"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Order: backend first (registers itself in CodingAgentBackend
    # registry), then the service that consumes it, then the plugin
    # entry point.
    for _mod_name in (
        "opencode_backend",
        "claude_code_backend",
        "code_conduit_service",
        "plugin",
    ):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
