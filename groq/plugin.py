"""Groq plugin — registers the Groq AI and Groq Whisper backends."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class GroqPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backends."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="groq",
            version="1.0.0",
            description="Groq AI backend",
            provides=["groq_ai", "groq_whisper"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import groq_ai  # noqa: F401
        from . import groq_whisper  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return GroqPlugin()
