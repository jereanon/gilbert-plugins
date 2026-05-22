"""OpenAI plugin — registers the GPT-based AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class OpenAIPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="openai",
            version="1.0.0",
            description="OpenAI GPT AI backend",
            provides=["openai_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import openai_ai  # noqa: F401
        from . import openai_whisper  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return OpenAIPlugin()
