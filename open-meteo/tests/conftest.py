"""Register the open-meteo plugin as a Python package for tests.

Multi-module template: ``open_meteo_weather`` does
``from .weather_codes import ...`` so we register every internal
module under one ``gilbert_plugin_open_meteo`` package. **Do not**
pass ``submodule_search_locations`` to ``spec_from_file_location`` —
that breaks intra-plugin relative imports (see the unifi conftest
comment for the long form of the rationale).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_open_meteo"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Order matters — leaf modules first, then dependents.
    for _mod_name in (
        "weather_codes",
        "open_meteo_weather",
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

