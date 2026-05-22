"""openWakeWord wake-word detection plugin."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class OpenWakeWordPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="openwakeword",
            version="1.0.0",
            description="openWakeWord — local wake-word detection",
            provides=["openwakeword"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import oww_backend  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return OpenWakeWordPlugin()
