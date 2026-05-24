"""Tests for the Kokoro TTS backend."""

from __future__ import annotations


def test_module_imports() -> None:
    """The package shim from conftest.py makes the plugin importable."""
    import gilbert_plugin_kokoro  # noqa: F401
