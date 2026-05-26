"""Register the mentra plugin as a real package for tests.

Unlike most std-plugins (which are flat single-file modules), the
mentra plugin has nested sub-packages (``protocol/``, ``session/``,
``session/managers/``). Registering the top-level package WITH a
``submodule_search_locations`` path on its spec is what lets the
import machinery recurse into the subdirectories the normal way —
``from .session.session import MentraSession`` resolves through
Python's regular package-resolution path.

This is the pattern the plugin loader uses in production; we
replicate it here so the same import paths work under pytest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_mentra"

if _pkg_name not in sys.modules:
    # Register the package via spec_from_file_location with
    # ``submodule_search_locations`` pointed at the real directory.
    # This makes Python treat the whole tree as a regular package —
    # nested imports like ``from .session.session import MentraSession``
    # resolve through the standard finder/loader chain, no manual
    # per-submodule registration needed.
    _spec = importlib.util.spec_from_file_location(
        _pkg_name,
        _plugin_dir / "__init__.py",
        submodule_search_locations=[str(_plugin_dir)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules[_pkg_name] = _pkg
    _spec.loader.exec_module(_pkg)
