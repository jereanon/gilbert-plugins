"""Pytest plumbing for the voice-agent plugin.

Same pattern as ``tesseract/tests/conftest.py`` — make the plugin
importable as ``gilbert_plugin_voice_agent`` so the plugin loader's
intra-plugin imports resolve when tests run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _register_plugin_module() -> None:
    name = "gilbert_plugin_voice_agent"
    if name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        name,
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


_register_plugin_module()
