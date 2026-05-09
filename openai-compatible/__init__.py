# ruff: noqa: N999
# Plugin directory name has a dash ("openai-compatible"), which ruff's
# module-name rule flags as invalid. Gilbert's plugin loader imports
# this package via ``importlib.util.spec_from_file_location`` under
# the sanitized name ``gilbert_plugin_openai_compatible`` (see
# ``tests/conftest.py``), so the filesystem name is purely
# cosmetic — ignore the rule at the file level instead of adding a
# per-file-ignore to the root pyproject.toml that would need to
# hard-code the plugin path.
