# ruff: noqa: N999
# Plugin directory name has a dash ("discord-webhook"), which ruff's
# module-name rule flags as invalid. Gilbert's plugin loader imports
# every plugin via importlib's spec_from_file_location with a sanitized
# package name (``gilbert_plugin_discord_webhook``), so the on-disk
# directory name is never used as a Python identifier.

