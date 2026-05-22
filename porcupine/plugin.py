"""Porcupine wake-word detection plugin."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class PorcupinePlugin(Plugin):
    """Side-effect plugin: importing ``porcupine`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="porcupine",
            version="1.0.0",
            description="Porcupine wake-word detection",
            provides=["porcupine"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import porcupine  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return PorcupinePlugin()
