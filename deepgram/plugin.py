"""Deepgram streaming transcription plugin."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class DeepgramPlugin(Plugin):
    """Side-effect plugin: importing ``deepgram`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="deepgram",
            version="1.0.0",
            description="Deepgram streaming speech-to-text",
            provides=["deepgram"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import deepgram  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return DeepgramPlugin()
