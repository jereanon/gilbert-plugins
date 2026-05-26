"""Register the google plugin as a Python package for tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_google"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    for _mod_name in (
        "google_credentials",
        "google_auth",
        "google_directory",
        "gmail",
        "gdrive_documents",
        "google_calendar",
        "google_tasks",
        "plugin",
    ):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
            submodule_search_locations=[],
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
