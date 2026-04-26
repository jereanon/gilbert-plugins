"""Register the american-standard plugin as a Python package for tests.

The plugin uses relative imports (``from . import nexia_backend``), so
pytest needs to see the directory as a proper package. This conftest
registers it once before any test collection happens.

See ``unifi/tests/conftest.py`` for the long-form explanation of why we
do NOT pass ``submodule_search_locations`` to ``spec_from_file_location``
— in short, that flags each module as a package and makes
``from .nexia_backend import ...`` resolve to a duplicate copy under
``plugin.nexia_backend``, which breaks ``isinstance`` and exception
handling in subtle ways.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_american_standard"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Order matters — leaf modules first, then dependents.
    for _mod_name in ("nexia_backend", "plugin"):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
