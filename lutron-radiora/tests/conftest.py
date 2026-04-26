"""Register the lutron-radiora plugin as a Python package for tests.

This plugin uses relative imports (``from .bridge import ...``), so
pytest needs to see the directory as a proper package. This conftest
registers it once before any test collection happens.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_lutron_radiora"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Order matters — leaf modules (bridge) first, then dependents.
    # Do NOT pass ``submodule_search_locations`` here — the unifi conftest
    # has the long-form explanation, but in short: ``[]`` flags each
    # module as a package and makes ``from .bridge import ...`` resolve
    # to a second copy of bridge under ``lutron_lights.bridge``. Omitting
    # the kwarg keeps everything anchored to ``gilbert_plugin_lutron_radiora``.
    for _mod_name in ("bridge", "lutron_lights", "lutron_shades", "plugin"):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
