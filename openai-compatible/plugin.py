"""OpenAI-Compatible plugin — vendor-neutral Chat Completions AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class OpenAICompatiblePlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="openai-compatible",
            version="1.0.0",
            description=("Vendor-neutral OpenAI-Chat-Completions AI backend"),
            provides=["openai_compatible_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import openai_compatible_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return OpenAICompatiblePlugin()
