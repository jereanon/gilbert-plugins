# ruff: noqa: N999
# Plugin directory name has a dash ("open-meteo"), which ruff's
# module-name rule flags as invalid. Gilbert's plugin loader imports
# this package via ``importlib.util.spec_from_file_location`` under
# the sanitized name ``gilbert_plugin_open_meteo`` (see
# ``tests/conftest.py``), so the filesystem name is purely
# cosmetic — ignore the rule at the file level.

